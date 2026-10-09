"""Dedicated updater for islevetah-app only. No host shell or arbitrary containers."""
import hmac
import io
import json
import os
import re
import sqlite3
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
import docker

REPO = 'cp296944/islevetah'
NAME = 'islevetah-app'
STATE = Path('/state')
DATA = Path('/data')
TOKEN = os.environ.get('OTA_INTERNAL_TOKEN','')
LOCK = threading.Lock()
JOB = {'running':False,'message':'尚未執行更新'}
LATEST = None

def client():
    return docker.from_env(timeout=1800)

def github_json(path):
    req=Request('https://api.github.com/repos/'+REPO+'/'+path,headers={'User-Agent':'islevetah-ota','Accept':'application/vnd.github+json'})
    with urlopen(req,timeout=20) as response:
        return json.load(response)

def current_version(d=None):
    own=d is None
    d=d or client()
    try:
        return d.containers.get(NAME).image.labels.get('org.opencontainers.image.revision','development')
    finally:
        if own:d.close()

def status():
    current=current_version()
    return {'current':current,'latest':LATEST,'update_available':bool(LATEST and LATEST!=current),'job':dict(JOB)}

def progress(message, **extra):
    JOB.update(message=message,**extra)
    STATE.mkdir(parents=True,exist_ok=True)
    temp=STATE/'job.tmp'
    temp.write_text(json.dumps(JOB,ensure_ascii=False),encoding='utf-8')
    temp.replace(STATE/'job.json')

def download_source(sha, directory):
    if not re.fullmatch(r'[a-f0-9]{40}',sha):
        raise ValueError('Invalid source revision')
    req=Request('https://api.github.com/repos/'+REPO+'/tarball/'+sha,headers={'User-Agent':'islevetah-ota'})
    with urlopen(req,timeout=60) as response:
        raw=response.read(20*1024*1024+1)
    if len(raw)>20*1024*1024:
        raise ValueError('Source archive too large')
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        members=archive.getmembers()
        if sum(m.size for m in members)>80*1024*1024:
            raise ValueError('Expanded source too large')
        for member in members:
            parts=Path(member.name).parts
            if len(parts)<2:
                continue
            relative=Path(*parts[1:])
            if relative.is_absolute() or '..' in relative.parts or member.issym() or member.islnk() or not(member.isfile() or member.isdir()):
                raise ValueError('Unsafe archive entry')
            member.name=str(relative)
            archive.extract(member,path=directory,filter='data')
    if not (Path(directory)/'Dockerfile').is_file():
        raise ValueError('Missing Dockerfile')

def database_backup():
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    folder=DATA/'backups'
    folder.mkdir(mode=0o700,exist_ok=True)
    source=DATA/'inventory.db'
    target=folder/('inventory_'+stamp+'_'+os.urandom(3).hex()+'.db')
    if not source.is_file():
        raise ValueError('Database is missing; refusing to update')
    src=sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True)
    dst=sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close();src.close()
    os.chmod(target,0o600)
    return target

def database_restore(backup):
    target=DATA/'inventory.db'
    owner=target.stat()
    temporary=DATA/'inventory.restore'
    src=sqlite3.connect('file:'+str(backup)+'?mode=ro',uri=True)
    dst=sqlite3.connect(temporary)
    try:
        src.backup(dst)
    finally:
        src.close();dst.close()
    os.chown(temporary,owner.st_uid,owner.st_gid)
    os.chmod(temporary,owner.st_mode & 0o777)
    temporary.replace(target)
    for suffix in ('-wal','-shm','-journal'):
        (DATA/('inventory.db'+suffix)).unlink(missing_ok=True)

def wait_healthy(container, timeout=100):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        container.reload()
        state=container.attrs['State']
        if state.get('Health',{}).get('Status')=='healthy':
            return
        if state['Status'] in ('exited','dead'):
            break
        time.sleep(2)
    raise RuntimeError('New container did not pass health checks')

def replacement_options(old):
    old.reload()
    attrs=old.attrs
    cfg=attrs['Config'];host=attrs['HostConfig']
    networks=list(attrs['NetworkSettings']['Networks'])
    if not networks:
        raise RuntimeError('Existing app has no network')
    ports={key:[{'HostIp':entry.get('HostIp',''),'HostPort':entry['HostPort']} for entry in value] for key,value in host['PortBindings'].items() if value}
    published=[entry['HostPort'] for values in ports.values() for entry in values]
    if published!=['7788']:
        raise RuntimeError('App must publish only port 7788')
    mounts=[]
    for mount in attrs['Mounts']:
        if mount['Type']=='bind':
            mounts.append(docker.types.Mount(target=mount['Destination'],source=mount['Source'],type='bind',read_only=not mount['RW']))
        elif mount['Type']=='volume':
            mounts.append(docker.types.Mount(target=mount['Destination'],source=mount['Name'],type='volume',read_only=not mount['RW']))
    env=dict(value.split('=',1) for value in cfg.get('Env',[]) if '=' in value)
    # APP_VERSION comes from the new image, never from old environment.
    env.pop('APP_VERSION',None)
    return dict(name=NAME,detach=True,ports=ports,environment=env,mounts=mounts,network=networks[0],restart_policy=host.get('RestartPolicy',{'Name':'unless-stopped'}),read_only=host.get('ReadonlyRootfs',True),tmpfs=host.get('Tmpfs',{}),cap_drop=host.get('CapDrop',[]),security_opt=host.get('SecurityOpt',[]),user=cfg.get('User','10001:10001'),labels=cfg.get('Labels',{})),networks

def update(sha):
    d=None;old=None;new=None;backup=None;renamed=False;stopped=False
    try:
        progress('正在下載 GitHub 原始碼…',running=True,target=sha)
        d=client()
        with tempfile.TemporaryDirectory() as directory:
            download_source(sha,directory)
            progress('正在 NAS 建置新版，網站仍可使用…')
            image,_=d.images.build(path=directory,tag='islevetah-app:'+sha[:12],buildargs={'APP_VERSION':sha},rm=True,pull=True)
        old=d.containers.get(NAME)
        options,networks=replacement_options(old)
        progress('正在停止網站並備份資料庫…')
        old.stop(timeout=30);stopped=True
        backup=database_backup()
        rollback=NAME+'-rollback-'+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')+'-'+os.urandom(2).hex()
        old.rename(rollback);renamed=True
        progress('正在啟動新版並驗證健康狀態…',backup=str(backup),rollback=rollback)
        options['labels']={**options['labels'],'org.opencontainers.image.revision':sha}
        new=d.containers.run(image.id,**options)
        for network in networks[1:]:
            d.networks.get(network).connect(new)
        wait_healthy(new)
        progress('更新完成，帳號與盤點資料已保留。',running=False,result='success',finished_at=int(time.time()))
        # Retain the stopped old container for manual recovery.
    except Exception as e:
        recovered=False
        if old and stopped:
            try:
                if new:
                    new.remove(force=True)
                if backup:
                    database_restore(backup)
                if renamed:
                    old.rename(NAME)
                old.start()
                wait_healthy(old)
                recovered=True
            except Exception:
                progress('更新失敗且自動回復未完成，請透過 SSH 檢查容器。',running=False,result='recovery_failed',finished_at=int(time.time()))
        if JOB.get('result')!='recovery_failed':
            progress('更新失敗，已回復舊版。' if recovered else '新版準備失敗，網站未變更。',running=False,result='rolled_back' if recovered else 'failed',finished_at=int(time.time()))
        # Do not return Docker environment, credentials, or raw command output.
        print('OTA failure:',type(e).__name__,flush=True)
    finally:
        if d:
            d.close()
        LOCK.release()

class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data):
        raw=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
    def authorized(self):
        supplied=self.headers.get('Authorization','')
        return bool(TOKEN) and hmac.compare_digest(supplied,'Bearer '+TOKEN)
    def do_GET(self):
        if self.path=='/health':
            return self.reply(200,{'ok':True})
        if not self.authorized():
            return self.reply(403,{'error':'Unauthorized'})
        if self.path!='/status':
            return self.reply(404,{'error':'Not found'})
        try:
            return self.reply(200,status())
        except Exception:
            return self.reply(503,{'error':'無法讀取網站容器狀態'})
    def do_POST(self):
        global LATEST,JOB
        if not self.authorized():
            return self.reply(403,{'error':'Unauthorized'})
        if self.path=='/check':
            try:
                sha=github_json('commits/main')['sha']
                if not re.fullmatch('[a-f0-9]{40}',sha):
                    raise ValueError('Invalid revision')
                LATEST=sha
                return self.reply(200,status())
            except Exception:
                return self.reply(503,{'error':'無法檢查 GitHub 版本，請稍後重試'})
        if self.path=='/apply':
            if not LATEST:
                return self.reply(400,{'error':'請先檢查新版'})
            if not LOCK.acquire(blocking=False):
                return self.reply(409,{'error':'已有更新正在執行'})
            try:
                if LATEST==current_version():
                    LOCK.release()
                    return self.reply(409,{'error':'目前已是最新版'})
            except Exception:
                LOCK.release()
                return self.reply(503,{'error':'無法讀取目前版本'})
            JOB={'running':True,'message':'已排入更新，準備下載…','started_at':int(time.time()),'target':LATEST}
            threading.Thread(target=update,args=(LATEST,),daemon=True).start()
            return self.reply(202,{'ok':True})
        return self.reply(404,{'error':'Not found'})

if __name__=='__main__':
    if not TOKEN:
        raise SystemExit('OTA_INTERNAL_TOKEN is required')
    STATE.mkdir(parents=True,exist_ok=True)
    if (STATE/'job.json').is_file():
        try:
            JOB=json.loads((STATE/'job.json').read_text(encoding='utf-8'))
            if JOB.get('running'):
                JOB.update(running=False,result='interrupted',message='上次更新程序中斷，請先透過 SSH 確認容器狀態。')
        except (ValueError,OSError):
            pass
    ThreadingHTTPServer(('0.0.0.0',7790),Handler).serve_forever()
