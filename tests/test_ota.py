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
        self.patches=[patch.object(u,'STATE',Path(self.temp.name)/'state'),patch.object(u,'DATA',Path(self.temp.name)/'data'),patch.object(u,'JOB',{'id':'test-job','running':True,'message':''})]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        u.DATA.mkdir()
    def container(self):
        old=MagicMock()
        old.attrs={'Config':{'Env':['APP_VERSION=old','OTA_INTERNAL_TOKEN=secret','INVENTORY_DB=/data/inventory.db'],'User':'10001:10001','Labels':{'org.opencontainers.image.source':u.SOURCE}},'HostConfig':{'PortBindings':{'7788/tcp':[{'HostIp':'','HostPort':'7788'}]},'RestartPolicy':{'Name':'unless-stopped'},'ReadonlyRootfs':True,'Tmpfs':{'/tmp':''},'CapDrop':['ALL'],'SecurityOpt':['no-new-privileges']},'NetworkSettings':{'Networks':{'islevetah_default':{}}},'Mounts':[{'Type':'bind','Source':'/volume3/islevet/data','Destination':'/data','RW':True}]}
        return old
    def run_update(self,healthy=True):
        old=self.container();new=MagicMock();d=MagicMock();d.containers.get.return_value=old;d.containers.run.return_value=new;d.images.list.return_value=[]
        backup=u.DATA/'backup.db';backup.write_bytes(b'backup')
        u.LOCK.acquire()
        with patch.object(u,'client',return_value=d),patch.object(u,'pull_image',return_value=MagicMock(id='new-image')),patch.object(u,'database_backup',return_value=backup),patch.object(u,'database_restore') as restore,patch.object(u,'urlopen',return_value=MagicMock(__enter__=lambda self:MagicMock(status=200))),patch.object(u,'wait_healthy',side_effect=None if healthy else [RuntimeError('failed'),None]):
            u.update('a'*40)
            return old,new,d,restore
    def test_success_preserves_configuration_and_removes_old_after_health(self):
        old,new,d,restore=self.run_update()
        options=d.containers.run.call_args.kwargs
        self.assertEqual(options['ports']['7788/tcp'][0]['HostPort'],'7788')
        self.assertEqual(options['environment']['OTA_INTERNAL_TOKEN'],'secret')
        self.assertNotIn('APP_VERSION',options['environment'])
        self.assertEqual(options['user'],'10001:10001')
        self.assertEqual(u.JOB['result'],'success')
        self.assertFalse(u.JOB['running'])
        old.remove.assert_called_once_with();restore.assert_not_called()
        self.assertFalse(u.LOCK.locked())
    def test_failed_health_restores_database_and_old_container(self):
        old,new,d,restore=self.run_update(False)
        new.remove.assert_called_once_with(force=True)
        restore.assert_called_once()
        old.start.assert_called_once()
        self.assertEqual(old.rename.call_args.args,('islevetah-app',))
        self.assertEqual(u.JOB['result'],'rolled_back')
        self.assertFalse(u.LOCK.locked())
    def test_download_failure_does_not_stop_live_site(self):
        d=MagicMock();d.images.list.return_value=[]
        u.LOCK.acquire()
        with patch.object(u,'client',return_value=d),patch.object(u,'pull_image',side_effect=RuntimeError('download error')):
            u.update('a'*40)
        d.containers.get.assert_not_called()
        self.assertEqual(u.JOB['result'],'failed')
    def test_unexpected_port_is_rejected(self):
        old=self.container();old.attrs['HostConfig']['PortBindings']['7788/tcp'][0]['HostPort']='1688'
        with self.assertRaises(RuntimeError):u.replacement_options(old)
    def test_check_queries_metadata_without_pull(self):
        d=MagicMock()
        d.images.get_registry_data.return_value.attrs={'Descriptor':{'digest':'sha256:'+'a'*64}}
        with patch.object(u,'client',return_value=d):
            self.assertEqual(u.remote_digest(),'sha256:'+'a'*64)
        d.api.pull.assert_not_called()

    def test_pull_is_pinned_and_tracks_bytes(self):
        d=MagicMock();d.api.pull.return_value=[{'id':'a','status':'Downloading','progressDetail':{'current':30}},{'id':'b','status':'Downloading','progressDetail':{'current':20}},{'id':'a','status':'Downloading','progressDetail':{'current':50}}]
        u.pull_image(d,'sha256:'+'a'*64)
        self.assertEqual(u.JOB['download_bytes'],70)
        self.assertEqual(d.api.pull.call_args.args[0],u.IMAGE+'@sha256:'+'a'*64)

    def test_preservation_failure_never_stops_app(self):
        d=MagicMock();old=self.container();old.image.tag.return_value=False;d.containers.get.return_value=old
        u.LOCK.acquire()
        with patch.object(u,'client',return_value=d),patch.object(u,'pull_image'):
            u.update('sha256:'+'a'*64)
        old.stop.assert_not_called()
        self.assertEqual(u.JOB['result'],'failed')

    def test_cleanup_excludes_used_rollback_manual_and_foreign(self):
        def image(id,tags,managed=True):
            x=MagicMock();x.id=id;x.tags=tags;x.labels={'io.islevetah.ota.managed':'app','org.opencontainers.image.source':u.SOURCE} if managed else {};x.attrs={'Size':100};return x
        d=MagicMock();used=image('used',[]);container=MagicMock();container.image=used;d.containers.list.return_value=[container]
        d.images.list.return_value=[used,image('rollback',['islevetah-app:rollback-x']),image('manual',['keep:forever']),image('foreign',[],False),image('eligible',[u.IMAGE+':sha-old'])]
        self.assertEqual(u.cleanup_preview(d)['images'],[{'id':'eligible','size':100}])
        self.assertEqual(u.cleanup(d),['eligible'])
        d.images.remove.assert_called_once_with('eligible',force=False)

    def test_restart_persists_interrupted_task(self):
        u.JOB.update(id='a'*32,started_at=1)
        u.progress('working')
        u.restore_state()
        self.assertFalse(u.JOB['running'])
        self.assertEqual(u.JOB['result'],'interrupted')
        self.assertEqual(json.loads((u.STATE/('a'*32+'.json')).read_text(encoding='utf-8'))['result'],'interrupted')

    def test_backup_failure_restarts_old_without_restoring_database(self):
        d=MagicMock();old=self.container();d.containers.get.return_value=old
        u.LOCK.acquire()
        with patch.object(u,'client',return_value=d),patch.object(u,'pull_image'),patch.object(u,'database_backup',side_effect=RuntimeError('backup failed')),patch.object(u,'database_restore') as restore,patch.object(u,'wait_healthy'):
            u.update('sha256:'+'a'*64)
        old.start.assert_called_once()
        restore.assert_not_called()
        d.containers.run.assert_not_called()
        self.assertEqual(u.JOB['result'],'rolled_back')

    def test_password_hash_not_plaintext(self):
        import server
        hashed=server.password_hash('123456')
        self.assertNotIn('123456',hashed)
        self.assertTrue(server.password_matches('123456',hashed))
        self.assertFalse(server.password_matches('654321',hashed))
        with self.assertRaises(server.APIError):server.password_hash('12345')

if __name__=='__main__':unittest.main()
