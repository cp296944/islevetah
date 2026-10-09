"""Dedicated updater for islevetah-app only. No host shell or arbitrary containers."""
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import uuid
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
IMAGE = 'ghcr.io/cp296944/islevetah-app'
SOURCE = 'https://github.com/' + REPO
JOB_LOCK = threading.RLock()

def client():
    return docker.from_env(timeout=1800)

def current_version(d=None):
    own=d is None
    d=d or client()
    try:
        image=d.containers.get(NAME).image
        digests=image.attrs.get('RepoDigests',[])
        return next((v.split('@',1)[1] for v in digests if v.startswith(IMAGE+'@')), image.id)
    finally:
        if own:d.close()

def status():
    current=current_version()
    with JOB_LOCK:
        job=dict(JOB)
    if job.get('started_at'):
        job['elapsed_seconds']=max(0,job.get('finished_at',int(time.time()))-job['started_at'])
    return {'current':current,'latest':LATEST,'update_available':bool(LATEST and LATEST!=current),'job':job}

def progress(message, **extra):
    with JOB_LOCK:
        JOB.update(message=message,**extra)
        STATE.mkdir(parents=True,exist_ok=True)
        temp=STATE/'job.tmp'
        temp.write_text(json.dumps(JOB,ensure_ascii=False),encoding='utf-8')
        temp.replace(STATE/'job.json')
        if re.fullmatch(r'[a-f0-9]{32}',JOB.get('id','')):
            archive=STATE/(JOB['id']+'.tmp')
            archive.write_text(json.dumps(JOB,ensure_ascii=False),encoding='utf-8')
            archive.replace(STATE/(JOB['id']+'.json'))

def remote_digest():
    d=client()
    try:
        digest=d.images.get_registry_data(IMAGE+':latest').attrs['Descriptor']['digest']
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',digest):
            raise ValueError('Invalid registry digest')
        return digest
    finally:d.close()

def pull_image(d, digest):
    layers={}
    for event in d.api.pull(IMAGE+'@'+digest,stream=True,decode=True):
        if event.get('error'):
            raise RuntimeError('Registry download failed')
        detail=event.get('progressDetail',{})
        if event.get('status')=='Downloading' and 'current' in detail:
            layers[event.get('id','')]=detail['current']
            progress('正在下載映像…',download_bytes=sum(layers.values()))
    return d.images.get(IMAGE+'@'+digest)

def cleanup_preview(d):
    used={c.image.id for c in d.containers.list(all=True)}
    candidates=[];manual=[]
    for image in d.images.list():
        tags=image.tags or []
        if image.id in used or any(t.startswith('islevetah-app:rollback') for t in tags):continue
        if image.labels.get('io.islevetah.ota.managed')!='app' or image.labels.get('org.opencontainers.image.source')!=SOURCE:
            if image.labels.get('org.opencontainers.image.source')==SOURCE:
                manual.append(image.id)
            continue
        allowed=lambda t: t.startswith(IMAGE+':sha-') or t==IMAGE+':latest'
        if any(not allowed(t) for t in tags):continue
        candidates.append({'id':image.id,'size':image.attrs.get('Size',0)})
    return {'images':candidates,'manual_review':manual,'estimated_bytes':sum(i['size'] for i in candidates),'note':'共用映像層可能使實際釋放空間小於預估。'}

def cleanup(d):
    removed=[]
    for item in cleanup_preview(d)['images']:
        # Recheck before each deletion; Docker refuses images used by containers.
        if item['id'] not in {i['id'] for i in cleanup_preview(d)['images']}:continue
        try:
            d.images.remove(item['id'],force=False)
            removed.append(item['id'])
        except docker.errors.APIError:pass
    return removed

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
    networks=attrs['NetworkSettings']['Networks']
    if not networks:
        raise RuntimeError('Existing app has no network')
    if cfg.get('Labels',{}).get('org.opencontainers.image.source')!=SOURCE:
        raise RuntimeError('Existing container does not belong to this system')
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
        else:
            raise RuntimeError('Unsupported mount; use independent deployment')
    env=dict(value.split('=',1) for value in cfg.get('Env',[]) if '=' in value)
    # APP_VERSION comes from the new image, never from old environment.
    env.pop('APP_VERSION',None)
    return dict(name=NAME,detach=True,ports=ports,environment=env,mounts=mounts,network=next(iter(networks)),networking_config={next(iter(networks)):docker.types.EndpointConfig(version='1.44',aliases=networks[next(iter(networks))].get('Aliases') or [])},restart_policy=host.get('RestartPolicy',{'Name':'unless-stopped'}),read_only=host.get('ReadonlyRootfs',True),tmpfs=host.get('Tmpfs',{}),cap_drop=host.get('CapDrop',[]),security_opt=host.get('SecurityOpt',[]),user=cfg.get('User','10001:10001'),labels=cfg.get('Labels',{})),networks

def update(sha):
    d=None;old=None;new=None;backup=None;renamed=False;stopped=False
    try:
        progress('正在下載映像…',running=True,target=sha,stage='downloading',completed_services=0)
        d=client()
        image=pull_image(d,sha)
        old=d.containers.get(NAME)
        options,networks=replacement_options(old)
        progress('正在保留上一版…',stage='preserving',rollback_image='islevetah-app:rollback-'+JOB['id'])
        if not old.image.tag('islevetah-app',tag='rollback-'+JOB['id']):
            raise RuntimeError('Unable to preserve previous image')
        progress('正在停止網站並備份資料庫…',stage='restarting')
        old.stop(timeout=30);stopped=True
        backup=database_backup()
        rollback=NAME+'-rollback-'+datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')+'-'+os.urandom(2).hex()
        old.rename(rollback);renamed=True
        progress('正在啟動新版並驗證健康狀態…',stage='verifying',backup=str(backup),rollback=rollback)
        new=d.containers.run(image.id,**options)
        for network in list(networks)[1:]:
            d.networks.get(network).connect(new,aliases=networks[network].get('Aliases') or [])
        wait_healthy(new)
        # Verify the actual public entry before discarding the old container.
        with urlopen('http://'+NAME+':7788/',timeout=10) as response:
            if response.status!=200:raise RuntimeError('Website entry check failed')
        old.remove()
        progress('更新完成，帳號與盤點資料已保留。',running=False,stage='completed',completed_services=1,result='success',finished_at=int(time.time()))
        # Keep exactly the most recent rollback tag; respect manually retained tags.
        try:
            keep='islevetah-app:rollback-'+JOB['id']
            for previous in d.images.list():
                for tag in previous.tags or []:
                    if previous.labels.get('org.opencontainers.image.source')==SOURCE and re.fullmatch(r'islevetah-app:rollback-[a-f0-9]{32}',tag) and tag!=keep:
                        d.images.remove(tag,force=False)
            progress(JOB['message'],cleanup_removed=cleanup(d))
        except Exception:
            progress(JOB['message'],cleanup_error='映像清理未完成，可重新預覽及執行。')
    except Exception as e:
        recovered=False
        progress('更新失敗，正在確認還原…',error={'APIError':'Docker 操作失敗，請檢查映像存取權限與容器狀態','NotFound':'找不到映像或網站容器','URLError':'網站入口無法連線','TimeoutError':'操作逾時'}.get(type(e).__name__,str(e)[:200] if isinstance(e,(RuntimeError,ValueError)) else type(e).__name__),stage='failed')
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
            except Exception as recovery_error:
                progress('正在記錄還原失敗…',recovery_error=type(recovery_error).__name__)
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
        if self.path.startswith('/jobs/'):
            job_id=self.path[6:]
            if not re.fullmatch(r'[a-f0-9]{32}',job_id):return self.reply(400,{'error':'Invalid job ID'})
            try:return self.reply(200,json.loads((STATE/(job_id+'.json')).read_text(encoding='utf-8')))
            except FileNotFoundError:return self.reply(404,{'error':'Job not found'})
        if self.path=='/cleanup-preview':
            d=client()
            try:return self.reply(200,cleanup_preview(d))
            except Exception:return self.reply(503,{'error':'無法讀取映像清理預覽'})
            finally:d.close()
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
                sha=remote_digest()
                LATEST=sha
                return self.reply(200,status())
            except Exception:
                return self.reply(503,{'error':'無法檢查 GHCR 映像（請確認套件公開及網路），請稍後重試'})
        if self.path=='/cleanup':
            if JOB.get('result') in ('interrupted','recovery_failed'):return self.reply(409,{'error':'請先人工確認中斷更新狀態，再執行清理'})
            if not LOCK.acquire(blocking=False):return self.reply(409,{'error':'已有更新或清理正在執行'})
            d=None
            try:
                d=client()
                return self.reply(200,{'removed':cleanup(d)})
            except Exception:return self.reply(503,{'error':'清理失敗，請重新預覽'})
            finally:
                if d:d.close()
                LOCK.release()
        if self.path=='/apply':
            if JOB.get('result') in ('interrupted','recovery_failed'):
                return self.reply(409,{'error':'請先透過 SSH 確認容器及資料庫，並重置中斷任務紀錄'})
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
            JOB={'id':uuid.uuid4().hex,'running':True,'message':'已排入更新，準備下載…','stage':'waiting','started_at':int(time.time()),'target':LATEST,'completed_services':0,'total_services':1,'download_bytes':0}
            try:
                progress(JOB['message'])
                threading.Thread(target=update,args=(JOB['target'],),daemon=True).start()
            except Exception:
                JOB.update(running=False,result='failed',message='無法儲存或啟動更新任務')
                LOCK.release()
                return self.reply(503,{'error':JOB['message']})
            return self.reply(202,{'ok':True,'job_id':JOB['id']})
        return self.reply(404,{'error':'Not found'})

def restore_state():
    global JOB
    STATE.mkdir(parents=True,exist_ok=True)
    if (STATE/'job.json').is_file():
        try:
            JOB=json.loads((STATE/'job.json').read_text(encoding='utf-8'))
            if not isinstance(JOB,dict):raise ValueError('Invalid state')
            if JOB.get('running'):
                progress('上次更新程序中斷，請先透過 SSH 確認容器及資料庫狀態。',running=False,result='interrupted',stage='interrupted',finished_at=int(time.time()))
        except (ValueError,OSError):
            JOB={'running':False,'result':'interrupted','message':'無法讀取上次任務，請人工確認容器及資料庫。'}

if __name__=='__main__':
    if not TOKEN:
        raise SystemExit('OTA_INTERNAL_TOKEN is required')
    restore_state()
    ThreadingHTTPServer(('0.0.0.0',7790),Handler).serve_forever()
