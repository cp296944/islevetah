import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from ota import updater as u

class OTATests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patches=[patch.object(u,'STATE',Path(self.temp.name)/'state'),patch.object(u,'DATA',Path(self.temp.name)/'data'),patch.object(u,'JOB',{'running':True,'message':''})]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        u.DATA.mkdir()
    def container(self):
        old=MagicMock()
        old.attrs={'Config':{'Env':['APP_VERSION=old','OTA_INTERNAL_TOKEN=secret','INVENTORY_DB=/data/inventory.db'],'User':'10001:10001','Labels':{}},'HostConfig':{'PortBindings':{'7788/tcp':[{'HostIp':'','HostPort':'7788'}]},'RestartPolicy':{'Name':'unless-stopped'},'ReadonlyRootfs':True,'Tmpfs':{'/tmp':''},'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges']},'NetworkSettings':{'Networks':{'islevetah_default':{}}},'Mounts':[{'Type':'bind','Source':'/volume3/islevet/data','Destination':'/data','RW':True}]}
        return old
    def run_update(self,healthy=True):
        old=self.container();new=MagicMock();d=MagicMock();d.containers.get.return_value=old;d.containers.run.return_value=new;d.images.build.return_value=(MagicMock(id='new-image'),[])
        backup=u.DATA/'backup.db';backup.write_bytes(b'backup')
        u.LOCK.acquire()
        with patch.object(u,'client',return_value=d),patch.object(u,'download_source'),patch.object(u,'database_backup',return_value=backup),patch.object(u,'database_restore') as restore,patch.object(u,'wait_healthy',side_effect=None if healthy else [RuntimeError('failed'),None]):
            u.update('a'*40)
            return old,new,d,restore
    def test_success_preserves_configuration_and_old_container(self):
        old,new,d,restore=self.run_update()
        options=d.containers.run.call_args.kwargs
        self.assertEqual(options['ports']['7788/tcp'][0]['HostPort'],'7788')
        self.assertEqual(options['environment']['OTA_INTERNAL_TOKEN'],'secret')
        self.assertNotIn('APP_VERSION',options['environment'])
        self.assertEqual(options['user'],'10001:10001')
        self.assertEqual(u.JOB['result'],'success')
        self.assertFalse(u.JOB['running'])
        old.remove.assert_not_called();restore.assert_not_called()
        self.assertFalse(u.LOCK.locked())
    def test_failed_health_restores_database_and_old_container(self):
        old,new,d,restore=self.run_update(False)
        new.remove.assert_called_once_with(force=True)
        restore.assert_called_once()
        old.start.assert_called_once()
        self.assertEqual(old.rename.call_args.args,('islevetah-app',))
        self.assertEqual(u.JOB['result'],'rolled_back')
        self.assertFalse(u.LOCK.locked())
    def test_build_failure_does_not_stop_live_site(self):
        d=MagicMock();d.images.build.side_effect=RuntimeError('build error')
        u.LOCK.acquire()
        with patch.object(u,'client',return_value=d),patch.object(u,'download_source'):
            u.update('a'*40)
        d.containers.get.assert_not_called()
        self.assertEqual(u.JOB['result'],'failed')
    def test_unexpected_port_is_rejected(self):
        old=self.container();old.attrs['HostConfig']['PortBindings']['7788/tcp'][0]['HostPort']='1688'
        with self.assertRaises(RuntimeError):u.replacement_options(old)
    def test_archive_traversal_rejected(self):
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w:gz') as archive:
            m=tarfile.TarInfo('project/../../escape');m.size=1;archive.addfile(m,io.BytesIO(b'x'))
        response=MagicMock();response.__enter__.return_value=response;response.read.return_value=raw.getvalue()
        with patch.object(u,'urlopen',return_value=response),self.assertRaises(ValueError):
            u.download_source('a'*40,self.temp.name)
    def test_password_hash_not_plaintext(self):
        import server
        hashed=server.password_hash('123456')
        self.assertNotIn('123456',hashed)
        self.assertTrue(server.password_matches('123456',hashed))
        self.assertFalse(server.password_matches('654321',hashed))
        with self.assertRaises(server.APIError):server.password_hash('12345')

if __name__=='__main__':unittest.main()
