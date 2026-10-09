import argparse
import hmac
import hashlib
import re
import time
import getpass
from http.cookies import SimpleCookie
import json
import math
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
DB = Path(os.environ.get("INVENTORY_DB", str(ROOT / "data" / "inventory.db")))
COOKIE = 'islevetah_session'
PERMISSIONS = {'inventory.view', 'products.manage', 'counts.manage'}
DUMMY_HASH = hashlib.scrypt(b'dummy',salt=b'inventory-dummy',n=16384,r=8,p=1).hex()

class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

def password_hash(password):
    if not isinstance(password,str) or not 6 <= len(password) <= 256:
        raise APIError(400,'密碼須為 6 至 256 個字元，不要求大小寫或特殊符號。')
    salt = secrets.token_hex(16)
    hashed = hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
    return salt+':'+hashed

def password_matches(password, stored):
    if not isinstance(password,str) or len(password)>256:
        return False
    salt, hashed = stored.split(':')
    actual = hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
    return hmac.compare_digest(actual,hashed)

def public_user(row):
    return {k:row[k] for k in ['id','username','full_name','email','phone','admin','active','approval','must_change','permissions','created_at']}

def normalize_permissions(value):
    if not isinstance(value,list) or not all(isinstance(p,str) for p in value) or set(value)-PERMISSIONS:
        raise APIError(400,'無效的權限設定')
    perms=set(value)
    if perms & {'products.manage','counts.manage'}:
        perms.add('inventory.view')
    return json.dumps(sorted(perms))

def contact(data):
    if not all(isinstance(data.get(k),str) for k in ('username','full_name','email','phone')):
        raise APIError(400,'帳號、姓名、Email 與手機必須填寫有效文字。')
    username = str(data.get('username','')).strip()
    full_name = str(data.get('full_name','')).strip()
    email = str(data.get('email','')).strip()
    phone = str(data.get('phone','')).strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}',username):
        raise APIError(400,'帳號須為 3 至 64 個英數字或 _ . -。')
    if not full_name or len(full_name)>100:
        raise APIError(400,'姓名必填，且不得超過 100 個字元。')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(email)>254:
        raise APIError(400,'請填寫有效 Email，且不得超過 254 個字元。')
    if not re.fullmatch(r'[+0-9() -]{6,30}',phone) or len(re.sub(r'\D','',phone))<6:
        raise APIError(400,'請填寫有效手機號碼，至少包含 6 個數字，最多 30 個字元。')
    return username,full_name,email,phone

def audit(db, actor, action, target=''):
    db.execute('INSERT INTO audit(at,actor,action,target) VALUES(?,?,?,?)',(int(time.time()),actor,action,str(target)))

def create_admin(username, password, name):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}',username):
        raise APIError(400,'帳號格式不正確')
    hashed=password_hash(password)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM users WHERE admin=1 AND active=1').fetchone():
            raise APIError(409,'已有管理員，請透過網站帳戶管理新增。')
        db.execute("INSERT INTO users(username,password,full_name,admin,active,approval,must_change,created_at) VALUES(?,?,?,1,1,'approved',0,?)",(username,hashed,name or username,int(time.time())))
        audit(db,None,'initial_admin',username)


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

def connect():
    db = sqlite3.connect(DB, timeout=15, factory=Connection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db

def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, barcode TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, unit TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
        CREATE UNIQUE INDEX IF NOT EXISTS barcode_unique ON products(barcode) WHERE barcode != '';
        CREATE TABLE IF NOT EXISTS product_options(kind TEXT NOT NULL CHECK(kind IN ('unit','category')),value TEXT NOT NULL,PRIMARY KEY(kind,value));
        INSERT OR IGNORE INTO product_options SELECT 'unit',trim(unit) FROM products WHERE trim(unit)!='';
        INSERT OR IGNORE INTO product_options SELECT 'category',trim(category) FROM products WHERE trim(category)!='';
        CREATE TABLE IF NOT EXISTS counts(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), quantity REAL NOT NULL CHECK(quantity>=0), person TEXT NOT NULL, counted_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(product_id,counted_at));
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password TEXT NOT NULL,full_name TEXT NOT NULL,email TEXT NOT NULL DEFAULT '',phone TEXT NOT NULL DEFAULT '',admin INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 0,approval TEXT NOT NULL DEFAULT 'pending',must_change INTEGER NOT NULL DEFAULT 0,permissions TEXT NOT NULL DEFAULT '[]',created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts(key TEXT PRIMARY KEY,count INTEGER NOT NULL,until INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at INTEGER NOT NULL,actor INTEGER REFERENCES users(id),action TEXT NOT NULL,target TEXT NOT NULL DEFAULT '');
        """)
        columns = {r['name'] for r in db.execute('PRAGMA table_info(counts)')}
        if 'user_id' not in columns:
            db.execute('ALTER TABLE counts ADD COLUMN user_id INTEGER REFERENCES users(id)')

def parse_date(value):
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)

def estimate(records, now=None):
    now = now or datetime.now(TZ)
    rows = sorted(records, key=lambda r: parse_date(r['counted_at']))
    valid, excluded = [], 0
    for a, b in zip(rows, rows[1:]):
        days = (parse_date(b['counted_at']) - parse_date(a['counted_at'])).total_seconds()/86400
        if days <= 0:
            continue
        delta = a['quantity'] - b['quantity']
        if delta < 0:
            excluded += 1
        else:
            valid.append((delta, days))
    selected = valid[-6:]
    daily = sum(x[0] for x in selected)/sum(x[1] for x in selected) if selected else None
    latest = rows[-1] if rows else None
    depletion = None
    remaining = None
    status = 'unknown'
    if latest:
        if latest['quantity'] == 0:
            depletion = parse_date(latest['counted_at'])
            status = 'empty'
        elif daily and daily > 0:
            depletion = parse_date(latest['counted_at']) + timedelta(days=latest['quantity']/daily)
            status = 'overdue' if depletion <= now else ('warning' if (depletion-now).total_seconds() < 5*86400 else 'normal')
        elif daily == 0:
            status = 'stable'
        if depletion:
            remaining = (depletion-now).total_seconds()/86400
    return dict(latest=latest, daily=daily, weekly=daily*7 if daily is not None else None, depletion=depletion.isoformat() if depletion else None, remaining=remaining, status=status, valid_intervals=len(selected), excluded_intervals=excluded)

def ota_request(action):
    token=os.environ.get('OTA_INTERNAL_TOKEN')
    if not token:
        raise APIError(503,'此環境尚未設定 NAS OTA 服務')
    req=Request('http://ota:7790/'+action,headers={'Authorization':'Bearer '+token},data=b'{}' if action in ('check','apply','cleanup') else None)
    try:
        with urlopen(req,timeout=35) as response:
            return json.load(response)
    except HTTPError as e:
        try:
            message=json.load(e).get('error','OTA 操作失敗')
        except Exception:
            message='OTA 操作失敗'
        raise APIError(e.code,message)
    except (URLError, TimeoutError):
        raise APIError(503,'無法連線至 OTA 服務，請確認 NAS 容器狀態')

class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def token_digest(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie',''))
            raw=cookies[COOKIE].value if COOKIE in cookies else ''
        except Exception:
            raw=''
        return hashlib.sha256(raw.encode()).hexdigest()

    def current_user(self):
        with connect() as db:
            row=db.execute("SELECT u.*,s.csrf,s.token AS session_token FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>? AND u.active=1 AND u.approval='approved'",(self.token_digest(),int(time.time()))).fetchone()
        return dict(row) if row else None

    def require(self, user, permission=None):
        if not user:
            raise APIError(401,'請先登入')
        if user['must_change']:
            raise APIError(403,'請先修改暫時密碼')
        if permission and not (user['admin'] or permission in json.loads(user['permissions'])):
            raise APIError(403,'此帳號沒有操作權限')

    def session_response(self, data, cookie):
        self.send_response(200)
        self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        self.wfile.write(json.dumps(data,ensure_ascii=False).encode())

    def login(self,data):
        username=str(data.get('username','')).strip()[:64]
        password=data.get('password','')
        now=int(time.time())
        keys=['user:'+username,'ip:'+self.client_address[0]]
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for key in keys:
                row=db.execute('SELECT * FROM attempts WHERE key=?',(key,)).fetchone()
                if row and row['until']>now and row['count']>=(10 if key.startswith('user:') else 50):
                    raise APIError(429,'登入嘗試過多，請於 15 分鐘後再試。')
            user=db.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
            good=password_matches(password,user['password'] if user else '00000000000000000000000000000000:'+DUMMY_HASH)
            if not user or not good or not user['active'] or user['approval']!='approved':
                for key in keys:
                    db.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN until<=? THEN 1 ELSE count+1 END,until=CASE WHEN until<=? THEN excluded.until ELSE until END',(key,now+900,now,now))
                audit(db,None,'login_failed',username)
                # Return after commit so rate limits persist.
                failure=True
            else:
                failure=False
                raw=secrets.token_urlsafe(48)
                csrf=secrets.token_urlsafe(32)
                duration=30*86400 if data.get('remember') is True else 8*3600
                db.execute('DELETE FROM sessions WHERE expires<=?',(now,))
                db.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),user['id'],csrf,now+duration))
                db.execute('DELETE FROM attempts WHERE key=?',(keys[0],))
                audit(db,user['id'],'login',username)
        if failure:
            raise APIError(401,'帳號或密碼錯誤，或帳號尚未核准／已停用。')
        cookie=COOKIE+'='+raw+'; HttpOnly; SameSite=Strict; Path=/'
        if data.get('remember') is True:
            cookie+='; Max-Age='+str(duration)
        if os.environ.get('INVENTORY_SECURE_COOKIE')=='1':
            cookie+='; Secure'
        self.session_response({'user':public_user(user),'csrf':csrf},cookie)

    def account_action(self, user, data):
        action=data.get('action')
        if action=='create':
            fields=contact(data)
            hashed=password_hash(data.get('password'))
            with connect() as db:
                cur=db.execute("INSERT INTO users(username,full_name,email,phone,password,created_at,must_change,active,approval,admin,permissions) VALUES(?,?,?,?,?,?,1,1,'approved',?,?)",(*fields,hashed,int(time.time()),int(data.get('admin') is True),normalize_permissions(data.get('permissions',[]))))
                audit(db,user['id'],'user_create',cur.lastrowid)
            return
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            uid=int(data['id'])
            target=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            if not target:
                raise APIError(404,'找不到帳號')
            if action=='profile':
                fields=contact(data)
                if db.execute('SELECT 1 FROM users WHERE username=? AND id!=?',(fields[0],uid)).fetchone():
                    raise APIError(409,'帳號名稱已有人使用，請使用其他名稱。')
                db.execute('UPDATE users SET username=?,full_name=?,email=?,phone=? WHERE id=?',(*fields,uid))
                if fields[0]!=target['username']:
                    db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
                audit(db,user['id'],'user_profile',json.dumps({'id':uid,'old_username':target['username'],'username':fields[0]},ensure_ascii=False))
                return
            if action in ('approve','reject'):
                if target['approval']=='approved':
                    raise APIError(400,'已核准的帳號請使用啟用或停用管理')
                approval='approved' if action=='approve' else 'rejected'
                perms=normalize_permissions(data.get('permissions',[])) if action=='approve' else '[]'
                db.execute('UPDATE users SET approval=?,active=?,admin=0,permissions=? WHERE id=?',(approval,int(action=='approve'),perms,uid))
            elif action=='save':
                active=int(data.get('active') is True)
                admin=int(data.get('admin') is True)
                if target['approval']!='approved' and (active or admin):
                    raise APIError(400,'請先核准此帳號')
                if target['active'] and target['admin'] and not(active and admin):
                    if db.execute("SELECT count(*) FROM users WHERE active=1 AND admin=1 AND approval='approved'").fetchone()[0]<=1:
                        raise APIError(400,'至少須保留一位啟用管理員')
                db.execute('UPDATE users SET active=?,admin=?,permissions=? WHERE id=?',(active,admin,normalize_permissions(data.get('permissions',[])),uid))
            elif action=='reset':
                if target['approval']!='approved':
                    raise APIError(400,'請先核准此帳號')
                db.execute('UPDATE users SET password=?,must_change=1 WHERE id=?',(password_hash(data.get('password')),uid))
            else:
                raise APIError(400,'無效的帳號操作')
            db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            audit(db,user['id'],'user_'+action,uid)

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith('/api/'):
            try:
                user=self.current_user()
                if path=='/api/session':
                    return self.reply(200,{'authenticated':bool(user),'user':public_user(user) if user else None,'csrf':user['csrf'] if user else None})
                if path=='/api/health':
                    with connect() as db:
                        db.execute('SELECT 1').fetchone()
                    return self.reply(200,{'ok':True})
                if path in ('/api/ota/status','/api/ota/cleanup-preview'):
                    self.require(user)
                    if not user['admin']:
                        raise APIError(403,'僅管理員可查看更新')
                    return self.reply(200,ota_request('cleanup-preview' if path.endswith('/cleanup-preview') else 'status'))
                if path=='/api/accounts':
                    self.require(user)
                    if not user['admin']:
                        raise APIError(403,'僅管理員可管理帳戶')
                    with connect() as db:
                        users=[public_user(r) for r in db.execute('SELECT * FROM users ORDER BY created_at DESC,id DESC')]
                        events=[dict(r) for r in db.execute('SELECT a.*,u.username AS actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor ORDER BY a.id DESC LIMIT 50')]
                    return self.reply(200,{'users':users,'events':events})
                if path == '/api/state':
                    self.require(user,'inventory.view')
                    with connect() as db:
                        products = [dict(r) for r in db.execute('SELECT * FROM products ORDER BY id DESC')]
                        counts = [dict(r) for r in db.execute('SELECT * FROM counts ORDER BY counted_at DESC')]
                        options = {kind: [r['value'] for r in db.execute('SELECT value FROM product_options WHERE kind=? ORDER BY value',(kind,))] for kind in ('unit','category')}
                    for p in products:
                        p['estimate'] = estimate([r for r in counts if r['product_id']==p['id']])
                    return self.reply(200, dict(products=products, counts=counts, options=options))
                return self.reply(404, {'error':'找不到資料'})
            except APIError as e:
                return self.reply(e.status,{'error':e.message})
            except sqlite3.Error:
                return self.reply(503,{'error':'資料庫暫時無法使用'})
        assets = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if path not in assets:
            return self.reply(404, {'error':'找不到頁面'})
        raw = (ROOT/'public'/assets[path]).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', {'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[path])
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        path = urlparse(self.path).path
        # Same-origin browser requests only; protects cookie-authenticated writes.
        origin = self.headers.get('Origin')
        if not origin or origin not in {'http://'+self.headers.get('Host',''),'https://'+self.headers.get('Host','')}:
            return self.reply(403, {'error':'不允許跨來源操作'})
        try:
            length = int(self.headers.get('Content-Length',0))
            if not 0 <= length <= 1000000:
                raise ValueError('資料過大')
            data = json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data,dict):
                raise APIError(400,'請使用有效的 JSON 物件')
            if path=='/api/login':
                return self.login(data)
            if path=='/api/register':
                fields=contact(data)
                if data.get('password')!=data.get('confirm_password'):
                    raise APIError(400,'兩次密碼不一致')
                hashed=password_hash(data.get('password'))
                with connect() as db:
                    cur=db.execute('INSERT INTO users(username,full_name,email,phone,password,created_at) VALUES(?,?,?,?,?,?)',(*fields,hashed,int(time.time())))
                    audit(db,None,'user_register',cur.lastrowid)
                return self.reply(200,{'ok':True})
            user=self.current_user()
            if not user:
                raise APIError(401,'請先登入')
            csrf=self.headers.get('X-CSRF-Token','')
            if not hmac.compare_digest(csrf,user['csrf']):
                raise APIError(403,'操作驗證失效，請重新登入')
            if path=='/api/logout':
                with connect() as db:
                    db.execute('DELETE FROM sessions WHERE token=?',(user['session_token'],))
                    audit(db,user['id'],'logout')
                return self.session_response({'ok':True},COOKIE+'=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
            if path=='/api/password':
                if not password_matches(data.get('current',''),user['password']):
                    raise APIError(400,'目前密碼不正確')
                if data.get('password')!=data.get('confirm_password'):
                    raise APIError(400,'兩次新密碼不一致')
                if password_matches(data.get('password',''),user['password']):
                    raise APIError(400,'新密碼須與目前密碼不同')
                hashed=password_hash(data.get('password'))
                with connect() as db:
                    db.execute('UPDATE users SET password=?,must_change=0 WHERE id=?',(hashed,user['id']))
                    db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
                    audit(db,user['id'],'password_changed')
                return self.session_response({'ok':True},COOKIE+'=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
            self.require(user)
            if path in ('/api/ota/check','/api/ota/apply','/api/ota/cleanup'):
                if not user['admin']:
                    raise APIError(403,'僅管理員可執行更新')
                if path.endswith(('/apply','/cleanup')):
                    if not password_matches(data.get('password',''),user['password']):
                        raise APIError(400,'管理員密碼不正確')
                    with connect() as db:
                        audit(db,user['id'],'ota_cleanup' if path.endswith('/cleanup') else 'ota_apply')
                return self.reply(202 if path.endswith('/apply') else 200,ota_request(path.rsplit('/',1)[1]))
            if path=='/api/accounts':
                if not user['admin']:
                    raise APIError(403,'僅管理員可管理帳戶')
                self.account_action(user,data)
                return self.reply(200,{'ok':True})
            self.require(user,{'/api/products':'products.manage','/api/products/delete':'products.manage','/api/counts':'counts.manage'}.get(path,'invalid'))
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if path == '/api/products':
                    action = data.get('action')
                    if action not in (None,'create','update'):
                        raise APIError(400,'無效的商品操作')
                    if action == 'create' and data.get('id'):
                        raise APIError(400,'新增商品不得包含既有商品 ID')
                    if action == 'update' and not data.get('id'):
                        raise APIError(400,'編輯商品必須指定商品 ID')
                    fields = [str(data.get(k,'')).strip() for k in ['barcode','name','unit','category','location','note']]
                    if not fields[1] or not fields[2] or any(len(f)>1000 for f in fields):
                        raise ValueError('請填寫品名、單位，欄位不得超過 1000 字')
                    if data.get('id'):
                        old = db.execute('SELECT unit FROM products WHERE id=?',(int(data['id']),)).fetchone()
                        if not old:
                            raise ValueError('商品不存在')
                        if old['unit'] != fields[2] and db.execute('SELECT 1 FROM counts WHERE product_id=? LIMIT 1',(int(data['id']),)).fetchone():
                            raise ValueError('已有盤點紀錄不可更換單位')
                        db.execute('UPDATE products SET barcode=?,name=?,unit=?,category=?,location=?,note=?,active=? WHERE id=?', (*fields,int(bool(data.get('active',True))),int(data['id'])))
                    else:
                        db.execute('INSERT INTO products(barcode,name,unit,category,location,note,active) VALUES(?,?,?,?,?,?,?)',(*fields,int(bool(data.get('active',True)))))
                    for kind,value in [('unit',fields[2]),('category',fields[3])]:
                        if value:
                            db.execute('INSERT OR IGNORE INTO product_options(kind,value) VALUES(?,?)',(kind,value))
                elif path == '/api/products/delete':
                    pid = int(data['id'])
                    product = db.execute('SELECT name FROM products WHERE id=?',(pid,)).fetchone()
                    if not product:
                        raise APIError(404,'商品不存在或已刪除')
                    if db.execute('SELECT 1 FROM counts WHERE product_id=? LIMIT 1',(pid,)).fetchone():
                        raise APIError(409,'已有盤點紀錄的商品不可刪除，請改用停用以保留歷史紀錄')
                    db.execute('DELETE FROM products WHERE id=?',(pid,))
                    audit(db,user['id'],'product_delete',json.dumps({'id':pid,'name':product['name']},ensure_ascii=False))
                elif path == '/api/counts':
                    person = user['full_name'] or user['username']
                    at = parse_date(data['counted_at'])
                    if not person or len(person)>100:
                        raise ValueError('請填寫盤點人員姓名')
                    if at > datetime.now(TZ):
                        raise ValueError('盤點時間不能晚於現在')
                    items = data.get('items',[])
                    if not items:
                        raise ValueError('至少填寫一項盤點數量')
                    for item in items:
                        quantity = float(item['quantity'])
                        if not math.isfinite(quantity) or quantity < 0 or quantity > 1e12:
                            raise ValueError('數量必須是有效的非負數')
                        product = db.execute('SELECT active FROM products WHERE id=?',(int(item['product_id']),)).fetchone()
                        if not product or not product['active']:
                            raise ValueError('商品不存在或已停用')
                        db.execute('INSERT INTO counts(product_id,quantity,person,counted_at,note,created_at,user_id) VALUES(?,?,?,?,?,?,?)',(int(item['product_id']),quantity,person,at.isoformat(),str(data.get('note',''))[:1000],datetime.now(TZ).isoformat(),user['id']))
                else:
                    return self.reply(404, {'error':'找不到操作'})
                audit(db,user['id'],'inventory_write',path)
            self.reply(200,{'ok':True})
        except APIError as e:
            self.reply(e.status,{'error':e.message})
        except (ValueError, KeyError, TypeError, OverflowError):
            self.reply(400, {'error':'請確認必填欄位、數量與盤點日期；盤點時間不可晚於現在'})
        except sqlite3.IntegrityError:
            self.reply(409, {'error':'帳號或條碼已存在，或同商品在此時間已有盤點紀錄'})
        except sqlite3.Error:
            self.reply(500, {'error':'資料庫操作失敗，請稍後重試'})

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='嶼地特寵醫院盤點系統')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=7788)
    parser.add_argument('--init-admin',metavar='USERNAME')
    args = parser.parse_args()
    initialize()
    if args.init_admin:
        try:
            password=getpass.getpass('管理員密碼（6 至 256 個字元）：')
            if password!=getpass.getpass('再次輸入密碼：'):
                raise APIError(400,'兩次密碼不一致')
            create_admin(args.init_admin,password,args.init_admin)
            print('管理員已建立。')
        except APIError as e:
            raise SystemExit(e.message)
        raise SystemExit(0)
    print(f'嶼地特寵醫院盤點系統：http://{args.host}:{args.port}',flush=True)
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
