"""CI-only real Docker startup, GitHub OTA, backup and persistence verification."""
import json
import subprocess
import time
from http.cookiejar import CookieJar
from urllib.error import URLError
from urllib.request import Request,build_opener,HTTPCookieProcessor

BASE='http://127.0.0.1:7788'
client=build_opener(HTTPCookieProcessor(CookieJar()))
csrf=''
def call(path,data=None):
    req=Request(BASE+'/api/'+path,data=json.dumps(data).encode() if data is not None else None,headers={'Origin':BASE,'Content-Type':'application/json','X-CSRF-Token':csrf})
    with client.open(req,timeout=40) as response:return json.load(response)
def wait_health():
    for _ in range(60):
        try:
            if call('health')['ok']:return
        except (URLError,ConnectionError):pass
        time.sleep(2)
    raise RuntimeError('Container startup timed out')
wait_health()
for asset in ('/','/app.js','/style.css'):
    with client.open(BASE+asset,timeout=10) as response:
        assert response.status==200 and response.read(),asset
subprocess.run(['docker','exec','islevetah-app','python','-c',"import server; server.create_admin('ci_admin','1234','CI Admin')"],check=True)
session=call('login',{'username':'ci_admin','password':'1234'})
csrf=session['csrf']
call('products',{'name':'CI persistence sentinel','unit':'支'})
state=call('state');pid=state['products'][0]['id']
call('counts',{'counted_at':'2026-01-01T09:00:00+08:00','items':[{'product_id':pid,'quantity':50}]})
version=call('ota/check',{})
assert version['update_available'],version
assert len(version['latest'])==40
call('ota/apply',{'password':'1234'})
finished=None
for _ in range(240):
    try:
        status=call('ota/status')
        if not status['job']['running']:
            finished=status;break
    except (URLError,ConnectionError,TimeoutError):pass
    time.sleep(3)
assert finished and finished['job']['result']=='success',finished
assert finished['current']==version['latest'],finished
state=call('state')
assert state['products'][0]['name']=='CI persistence sentinel'
assert state['counts'][0]['quantity']==50
assert call('session')['authenticated']
backup=subprocess.check_output(['docker','exec','islevetah-ota','python','-c',"from pathlib import Path; print(len(list(Path('/data/backups').glob('inventory_*.db'))))"],text=True)
assert int(backup.strip())>=1
print('Real Docker startup, OTA replacement, SQLite backup, sessions and inventory persistence verified.')
