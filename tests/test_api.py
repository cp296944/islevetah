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
        os.environ['INVENTORY_PASSWORD']='integration-test-only'
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
        server.SESSIONS.clear()
    def call(self,path,data=None):
        req=Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
        try:
            with self.client.open(req) as r:
                return r.status,json.loads(r.read())
        except HTTPError as e:
            return e.code,json.loads(e.read())
    def login(self):
        self.assertEqual(self.call('/api/login',{'password':'integration-test-only'})[0],200)
    def product(self):
        self.assertEqual(self.call('/api/products',{'name':'30CC 針筒','unit':'支','barcode':'123'})[0],200)
        return self.call('/api/state')[1]['products'][0]['id']
    def test_authentication(self):
        self.assertEqual(self.call('/api/state')[0],401)
        self.assertEqual(self.call('/api/login',{'password':'wrong'})[0],401)
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

if __name__=='__main__': unittest.main()
