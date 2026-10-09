import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, build_opener, HTTPCookieProcessor
from urllib.error import HTTPError
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
import server

class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        server.DB = Path(cls.temp.name)/'test.db'

        server.initialize()
        cls.http = ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever,daemon=True)
        cls.thread.start()
        cls.url='http://127.0.0.1:'+str(cls.http.server_port)
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()
        cls.temp.cleanup()
    def setUp(self):
        self.client=build_opener(HTTPCookieProcessor(CookieJar()))
        with server.connect() as db:
            db.execute('DELETE FROM counts')
            db.execute('DELETE FROM products')
            db.execute('DELETE FROM sessions')
            db.execute('DELETE FROM audit')
            db.execute('DELETE FROM users')
            db.execute('DELETE FROM attempts')
        server.create_admin('admin','1234','管理員')
        self.csrf=''
    def call(self,path,data=None):
        req=Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json','Origin':self.url,'X-CSRF-Token':self.csrf})
        try:
            with self.client.open(req) as r:
                return r.status,json.loads(r.read())
        except HTTPError as e:
            return e.code,json.loads(e.read())
    def login(self):
        status,data=self.call('/api/login',{'username':'admin','password':'1234'})
        self.assertEqual(status,200)
        self.csrf=data['csrf']
    def product(self):
        self.assertEqual(self.call('/api/products',{'name':'30CC 針筒','unit':'支','barcode':'123'})[0],200)
        return self.call('/api/state')[1]['products'][0]['id']
    def test_authentication(self):
        self.assertEqual(self.call('/api/state')[0],401)
        self.assertEqual(self.call('/api/login',{'username':'admin','password':'wrong'})[0],401)
        self.login()
        self.assertTrue(self.call('/api/session')[1]['authenticated'])
        self.call('/api/logout',{})
        self.assertEqual(self.call('/api/state')[0],401)
    def test_count_and_forecast(self):
        self.login()
        pid=self.product()
        for at,qty in [('2026-05-01T09:00+08:00',50),('2026-05-08T09:00+08:00',20)]:
            self.assertEqual(self.call('/api/counts',{'person':'員工甲','counted_at':at,'items':[{'product_id':pid,'quantity':qty}]})[0],200)
        state=self.call('/api/state')[1]
        self.assertEqual(state['products'][0]['estimate']['weekly'],30)
        self.assertEqual(state['products'][0]['estimate']['latest']['quantity'],20)
        self.assertEqual(len(state['counts']),2)
        self.assertEqual(state['counts'][0]['person'],'管理員')
        self.assertEqual(self.call('/api/products',{'id':pid,'name':'30CC 針筒','unit':'盒'})[0],400)
    def test_duplicate_and_transaction_rollback(self):
        self.login()
        pid=self.product()
        self.assertEqual(self.call('/api/products',{'name':'重複','unit':'支','barcode':'123'})[0],409)
        payload={'person':'甲','counted_at':'2026-05-01T09:00+08:00','items':[{'product_id':pid,'quantity':10},{'product_id':pid,'quantity':-1}]}
        self.assertEqual(self.call('/api/counts',payload)[0],400)
        self.assertEqual(self.call('/api/state')[1]['counts'],[])
        payload['items']=[{'product_id':pid,'quantity':0}]
        self.assertEqual(self.call('/api/counts',payload)[0],200)
        self.assertEqual(self.call('/api/counts',payload)[0],409)
        self.assertEqual(self.call('/api/state')[1]['products'][0]['estimate']['status'],'empty')
    def test_future_and_nonfinite_rejected(self):
        self.login()
        pid=self.product()
        payload={'person':'甲','counted_at':'2999-05-01T09:00+08:00','items':[{'product_id':pid,'quantity':10}]}
        self.assertEqual(self.call('/api/counts',payload)[0],400)
        payload['counted_at']='2026-05-01T09:00+08:00'
        payload['items'][0]['quantity']='NaN'
        self.assertEqual(self.call('/api/counts',payload)[0],400)
    def test_cross_origin_blocked(self):
        self.login()
        req=Request(self.url+'/api/products',data=b'{}',headers={'Origin':'https://evil.example','Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as e:
            self.client.open(req)
        self.assertEqual(e.exception.code,403)
    def test_assets(self):
        for path in ['/','/style.css','/app.js']:
            with self.client.open(self.url+path) as r:
                self.assertEqual(r.status,200)
                self.assertTrue(r.read())

    def test_registration_and_approval(self):
        data={'username':'staff','full_name':'員工甲','email':'staff@example.com','phone':'0912345678','password':'1234','confirm_password':'1234','admin':True,'permissions':['products.manage']}
        self.assertEqual(self.call('/api/register',data)[0],200)
        self.assertEqual(self.call('/api/login',{'username':'staff','password':'1234'})[0],401)
        self.login()
        users=self.call('/api/accounts')[1]['users']
        staff=next(u for u in users if u['username']=='staff')
        self.assertEqual(staff['admin'],0)
        self.assertEqual(staff['permissions'],'[]')
        self.assertNotIn('password',staff)
        self.assertEqual(self.call('/api/accounts',{'action':'approve','id':staff['id'],'permissions':['counts.manage']})[0],200)
        self.call('/api/logout',{})
        status,session=self.call('/api/login',{'username':'staff','password':'1234'})
        self.assertEqual(status,200)
        self.csrf=session['csrf']
        self.assertEqual(self.call('/api/state')[0],200)
        self.assertEqual(self.call('/api/accounts')[0],403)
        self.assertEqual(self.call('/api/products',{'name':'無權限','unit':'支'})[0],403)
        self.assertEqual(self.call('/api/ota/check',{})[0],403)
    def test_password_minimum_and_forced_change(self):
        self.login()
        data={'action':'create','username':'staff','full_name':'員工','email':'staff@example.com','phone':'0912345678','password':'123','permissions':['inventory.view']}
        self.assertEqual(self.call('/api/accounts',data)[0],400)
        data['password']='1234'
        self.assertEqual(self.call('/api/accounts',data)[0],200)
        self.call('/api/logout',{})
        status,session=self.call('/api/login',{'username':'staff','password':'1234'})
        self.assertEqual(status,200)
        self.csrf=session['csrf']
        self.assertTrue(session['user']['must_change'])
        self.assertEqual(self.call('/api/state')[0],403)
        self.assertEqual(self.call('/api/password',{'current':'1234','password':'1234','confirm_password':'1234'})[0],400)
        self.assertEqual(self.call('/api/password',{'current':'1234','password':'5678','confirm_password':'5678'})[0],200)
        self.assertEqual(self.call('/api/state')[0],401)
        status,session=self.call('/api/login',{'username':'staff','password':'5678'})
        self.assertEqual(status,200)
        self.assertFalse(session['user']['must_change'])
    def test_last_admin_and_session_revocation(self):
        self.login()
        self.assertEqual(self.call('/api/accounts',{'action':'save','id':1,'admin':False,'active':True,'permissions':[]})[0],400)
        self.assertEqual(self.call('/api/accounts',{'action':'save','id':1,'admin':True,'active':False,'permissions':[]})[0],400)
        self.assertEqual(self.call('/api/accounts',{'action':'reset','id':1,'password':'5678'})[0],200)
        self.assertEqual(self.call('/api/state')[0],401)
    def test_csrf_and_missing_origin(self):
        self.login()
        csrf=self.csrf;self.csrf=''
        self.assertEqual(self.call('/api/products',{'name':'甲','unit':'支'})[0],403)
        self.csrf=csrf
        req=Request(self.url+'/api/products',data=b'{}',headers={'Content-Type':'application/json','X-CSRF-Token':csrf})
        with self.assertRaises(HTTPError) as error:
            self.client.open(req)
        self.assertEqual(error.exception.code,403)
    def test_persistent_session_and_remember(self):
        status,result=self.call('/api/login',{'username':'admin','password':'1234','remember':True})
        self.assertEqual(status,200)
        cookie=next(iter(self.client.handlers[0].cookiejar),None) if hasattr(self.client.handlers[0],'cookiejar') else None
        with server.connect() as db:
            session=db.execute('SELECT * FROM sessions').fetchone()
            self.assertNotIn('=',session['token'])
            self.assertEqual(len(session['token']),64)
            self.assertGreater(session['expires']-__import__('time').time(),29*86400)
        server.initialize()
        self.assertTrue(self.call('/api/session')[1]['authenticated'])
    def test_login_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.call('/api/login',{'username':'admin','password':'wrong'})[0],401)
        self.assertEqual(self.call('/api/login',{'username':'admin','password':'1234'})[0],429)
    def test_ota_requires_admin_password(self):
        self.login()
        self.assertEqual(self.call('/api/ota/apply',{'password':'wrong'})[0],400)
        self.assertEqual(self.call('/api/ota/check',{})[0],503)

if __name__=='__main__': unittest.main()
