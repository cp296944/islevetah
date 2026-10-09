import argparse
import hmac
import json
import math
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
DB = Path(os.environ.get("INVENTORY_DB", str(ROOT / "data" / "inventory.db")))
SESSIONS = {}

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
        CREATE TABLE IF NOT EXISTS counts(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), quantity REAL NOT NULL CHECK(quantity>=0), person TEXT NOT NULL, counted_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(product_id,counted_at));
        """)

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

class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        token = next((x.strip()[8:] for x in self.headers.get('Cookie','').split(';') if x.strip().startswith('session=')), '')
        entry = SESSIONS.get(token)
        return bool(entry and entry['expires'] > datetime.now(TZ))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/session':
            return self.reply(200, {'authenticated': self.authorized()})
        if path.startswith('/api/'):
            if not self.authorized():
                return self.reply(401, {'error':'請先登入'})
            if path == '/api/state':
                with connect() as db:
                    products = [dict(r) for r in db.execute('SELECT * FROM products ORDER BY id DESC')]
                    counts = [dict(r) for r in db.execute('SELECT * FROM counts ORDER BY counted_at DESC')]
                for p in products:
                    p['estimate'] = estimate([r for r in counts if r['product_id']==p['id']])
                return self.reply(200, dict(products=products, counts=counts))
            return self.reply(404, {'error':'找不到資料'})
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
        if origin and urlparse(origin).netloc != self.headers.get('Host'):
            return self.reply(403, {'error':'不允許跨來源操作'})
        try:
            length = int(self.headers.get('Content-Length',0))
            if length > 1000000:
                raise ValueError('資料過大')
            data = json.loads(self.rfile.read(length) or b'{}')
            if path == '/api/login':
                if not hmac.compare_digest(str(data.get('password','')), os.environ['INVENTORY_PASSWORD']):
                    return self.reply(401, {'error':'密碼錯誤'})
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = dict(expires=datetime.now(TZ)+timedelta(hours=12))
                self.send_response(200)
                self.send_header('Set-Cookie', 'session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200')
                self.send_header('Content-Type','application/json')
                self.end_headers()
                self.wfile.write(b'{}')
                return
            if not self.authorized():
                return self.reply(401, {'error':'請先登入'})
            if path == '/api/logout':
                for item in self.headers.get('Cookie','').split(';'):
                    if item.strip().startswith('session='):
                        SESSIONS.pop(item.strip()[8:],None)
                return self.reply(200,{})
            with connect() as db:
                if path == '/api/products':
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
                        db.execute('INSERT INTO products(barcode,name,unit,category,location,note) VALUES(?,?,?,?,?,?)',fields)
                elif path == '/api/counts':
                    person = str(data.get('person','')).strip()
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
                        db.execute('INSERT INTO counts(product_id,quantity,person,counted_at,note,created_at) VALUES(?,?,?,?,?,?)',(int(item['product_id']),quantity,person,at.isoformat(),str(data.get('note',''))[:1000],datetime.now(TZ).isoformat()))
                else:
                    return self.reply(404, {'error':'找不到操作'})
            self.reply(200,{'ok':True})
        except (ValueError, KeyError, TypeError, OverflowError):
            self.reply(400, {'error':'請確認必填欄位、數量與盤點日期；盤點時間不可晚於現在'})
        except sqlite3.IntegrityError:
            self.reply(409, {'error':'條碼已存在，或同商品在此時間已有盤點紀錄'})
        except sqlite3.Error:
            self.reply(500, {'error':'資料庫操作失敗，請稍後重試'})

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='嶼地特寵醫院盤點系統')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    args = parser.parse_args()
    if not os.environ.get('INVENTORY_PASSWORD'):
        raise SystemExit('請先設定 INVENTORY_PASSWORD 環境變數作為登入密碼')
    initialize()
    print(f'嶼地特寵醫院盤點系統：http://{args.host}:{args.port}',flush=True)
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
