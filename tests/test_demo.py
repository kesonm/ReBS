import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.demo import DemoError, DemoService, sign
from backend.okx import OKXService
from backend.service import InputError

ENV={'OKX_DEMO_API_KEY':'test-demo-key','OKX_DEMO_SECRET_KEY':'test-demo-secret',
     'OKX_DEMO_PASSPHRASE':'test-demo-passphrase','REBS_DEMO_ACCESS_TOKEN':'test-access-token-0123456789'}
ORDER={'instId':'BTC-USDT','side':'buy','ordType':'limit','px':'60000.1','sz':'0.001','clOrdId':'a'*32}


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.environment=patch.dict(os.environ,{**ENV,'OKX_REST_URL':'https://openapi.okx.com',
            'REBS_DEMO_STATE_PATH':str(Path(self.temporary.name)/'ledger.sqlite3')})
        self.environment.start()
        self.requests=[]
        self.handler=self.default_response
        async def handle(request):
            self.requests.append(request)
            return self.handler(request)
        self.market=OKXService(httpx.AsyncClient(base_url='https://openapi.okx.com',transport=httpx.MockTransport(handle)))
        self.demo=DemoService(self.market)

    def default_response(self,request):
        path=request.url.path
        if path.endswith('/public/time'): rows=[{'ts':str(int(time.time()*1000))}]
        elif path.endswith('/instruments'):
            rows=[{'instId':'BTC-USDT','state':'live','tickSz':'0.1','lotSz':'0.00001','minSz':'0.0001'}]
        elif request.method=='POST': rows=[{'sCode':'0','ordId':'123','clOrdId':'a'*32}]
        elif path.endswith('/balance'):
            rows=[{'totalEq':'12000.12','details':[{'ccy':'USDT','eq':'10000','availBal':'9990','secret':'must-not-leak'}]}]
        elif path.endswith('/order'): rows=[{'ordId':'123','state':'filled'}]
        else: rows=[]
        return httpx.Response(200,json={'code':'0','data':rows})

    async def asyncTearDown(self):
        await self.market.close()
        self.environment.stop()
        self.temporary.cleanup()

    def writes(self): return [r for r in self.requests if r.method=='POST']

    def assert_signed(self,request):
        timestamp=request.headers['OK-ACCESS-TIMESTAMP']
        message=timestamp+request.method+request.url.raw_path.decode()+request.content.decode()
        expected=base64.b64encode(hmac.new(ENV['OKX_DEMO_SECRET_KEY'].encode(),message.encode(),hashlib.sha256).digest()).decode()
        self.assertEqual(request.headers['OK-ACCESS-SIGN'],expected)
        self.assertEqual(request.headers['x-simulated-trading'],'1')

    async def test_read_signs_exact_query_and_never_sets_client_defaults(self):
        await self.demo.read('/api/v5/trade/orders-pending',{'instType':'SPOT','limit':'100'})
        request=self.requests[-1]
        self.assertEqual(str(request.url),'https://openapi.okx.com/api/v5/trade/orders-pending?instType=SPOT&limit=100')
        self.assert_signed(request)
        self.assertNotIn('OK-ACCESS-KEY',self.market.client.headers)
        self.assertNotIn('OK-ACCESS-KEY',self.requests[0].headers)

    async def test_overview_whitelists_fields_and_keeps_missing_values(self):
        data=await self.demo.overview()
        self.assertEqual(data['environment'],'demo')
        self.assertEqual(data['totalEquity'],'12000.12')
        self.assertIsNone(data['availableEquity'])
        self.assertNotIn('secret',json.dumps(data))
        self.assertEqual(data['warnings'],[])

    async def test_partial_section_failure_is_explicit(self):
        def response(request):
            if request.url.path.endswith('/fills'): return httpx.Response(200,json={'code':'50120','data':[]})
            return self.default_response(request)
        self.handler=response
        data=await self.demo.overview()
        self.assertIsNone(data['fills'])
        self.assertEqual(data['warnings'][0]['section'],'成交记录')

    async def test_environment_mismatch_is_not_retried_or_falls_back_live(self):
        self.demo.clock_synced=time.monotonic()
        self.handler=lambda r:httpx.Response(200,json={'code':'50101','msg':ENV['OKX_DEMO_SECRET_KEY'],'data':[]})
        with self.assertRaisesRegex(DemoError,'模拟盘专用密钥') as caught:
            await self.demo.read('/api/v5/account/balance')
        self.assertNotIn(ENV['OKX_DEMO_SECRET_KEY'],str(caught.exception))
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.requests[0].headers['x-simulated-trading'],'1')

    async def test_get_timestamp_recalibrates_but_post_timestamp_error_never_retries(self):
        count=0
        def response(request):
            nonlocal count
            if request.url.path.endswith('/balance'):
                count+=1
                if count==1: return httpx.Response(200,json={'code':'50102','data':[]})
            if request.method=='POST': return httpx.Response(200,json={'code':'50102','data':[]})
            return self.default_response(request)
        self.handler=response
        await self.demo.read('/api/v5/account/balance')
        self.assertEqual(count,2)
        self.assertEqual(len([r for r in self.requests if r.url.path.endswith('/public/time')]),2)
        result=await self.demo.place(ORDER)
        self.assertEqual(result['outcome'],'rejected')
        self.assertEqual(len(self.writes()),1)

    async def test_missing_keys_never_send_any_private_request(self):
        self.demo.key=''
        with self.assertRaises(DemoError): await self.demo.read('/api/v5/account/balance')
        self.assertEqual(self.requests,[])

    async def test_untrusted_host_and_nonallowed_endpoint_rejected_before_signing(self):
        for url in ['http://www.okx.com','https://www.okx.com.attacker.test','https://ws.okx.com']:
            self.market.base_url=url
            with self.assertRaises(DemoError): await self.demo.read('/api/v5/account/balance')
        with self.assertRaises(DemoError): await self.demo.request('POST','/api/v5/asset/withdrawal',body={})
        self.assertEqual(self.requests,[])

    async def test_limit_order_signed_cash_and_persistent_idempotency(self):
        first=await self.demo.place(ORDER)
        self.assertEqual(first['outcome'],'accepted')
        request=self.writes()[0]
        self.assert_signed(request)
        body=json.loads(request.content)
        self.assertEqual(body['tdMode'],'cash')
        self.assertEqual(body['px'],'60000.1')
        self.assertIn('expTime',request.headers)
        # A fresh service object simulates a process restart.
        again=await DemoService(self.market).place(ORDER)
        self.assertEqual(again,first)
        self.assertEqual(len(self.writes()),1)
        with self.assertRaises(InputError): await self.demo.place({**ORDER,'sz':'0.002'})
        ledger=Path(self.demo.ledger_path).read_bytes()
        self.assertNotIn(ENV['OKX_DEMO_API_KEY'].encode(),ledger)
        self.assertNotIn(ENV['OKX_DEMO_SECRET_KEY'].encode(),ledger)

    async def test_parallel_double_click_posts_once(self):
        results=await asyncio.gather(self.demo.place(ORDER),self.demo.place(ORDER))
        self.assertTrue(all(r['outcome'] in {'accepted','confirmed'} for r in results))
        self.assertEqual(len(self.writes()),1)

    async def test_market_size_is_base_currency_without_auto_amend(self):
        await self.demo.place({**ORDER,'ordType':'market','px':None})
        body=json.loads(self.writes()[0].content)
        self.assertEqual(body['tgtCcy'],'base_ccy')
        self.assertTrue(body['banAmend'])
        self.assertNotIn('px',body)
        self.assert_signed(self.writes()[0])

    async def test_quantity_and_price_steps_use_decimal_and_no_network_write_on_invalid(self):
        for changed in [{'sz':'0.00001'},{'sz':'0.001001'},{'px':'60000.11'},{'sz':'NaN'},{'sz':'1e-3'},{'px':'0'}]:
            with self.subTest(changed=changed):
                with self.assertRaises(InputError): await self.demo.place({**ORDER,**changed})
        self.assertEqual(self.writes(),[])

    async def test_post_timeout_queries_without_reposting_even_after_restart(self):
        def response(request):
            if request.method=='POST': raise httpx.ReadTimeout('Timeout',request=request)
            if request.url.path.endswith('/order'): return httpx.Response(200,json={'code':'51603','data':[]})
            return self.default_response(request)
        self.handler=response
        result=await self.demo.place(ORDER)
        self.assertEqual(result['outcome'],'unknown')
        result=await DemoService(self.market).place(ORDER)
        self.assertEqual(result['outcome'],'unknown')
        self.assertEqual(len(self.writes()),1)

    async def test_timeout_after_accepted_can_be_reconciled(self):
        def response(request):
            if request.method=='POST': return httpx.Response(500,text='upstream failure')
            return self.default_response(request)
        self.handler=response
        result=await self.demo.place(ORDER)
        self.assertEqual(result['outcome'],'confirmed')
        self.assertEqual(result['state'],'filled')
        self.assertEqual(len(self.writes()),1)

    async def test_per_order_timeout_code_is_reconciled_not_marked_rejected(self):
        def response(request):
            if request.method=='POST': return httpx.Response(200,json={'code':'0','data':[{'sCode':'50004'}]})
            return self.default_response(request)
        self.handler=response
        result=await self.demo.place(ORDER)
        self.assertEqual(result['outcome'],'confirmed')
        self.assertEqual(len(self.writes()),1)

    async def test_scode_rejection_does_not_display_fake_success_or_leak_upstream_message(self):
        def response(request):
            if request.method=='POST': return httpx.Response(200,json={'code':'0','data':[{'sCode':'51008','sMsg':ENV['OKX_DEMO_API_KEY']}]})
            return self.default_response(request)
        self.handler=response
        result=await self.demo.place(ORDER)
        self.assertEqual(result['outcome'],'rejected')
        self.assertIn('余额不足',result['message'])
        self.assertNotIn(ENV['OKX_DEMO_API_KEY'],str(result))

    async def test_cancel_uses_demo_header_and_acknowledges_not_final_state(self):
        result=await self.demo.cancel('BTC-USDT','123')
        self.assertEqual(result['outcome'],'accepted')
        self.assertNotIn('state',result)
        self.assertIn('状态为准',result['message'])
        self.assertEqual(self.writes()[0].url.path,'/api/v5/trade/cancel-order')
        self.assert_signed(self.writes()[0])


class DemoAPITests(unittest.TestCase):
    def test_public_status_never_exposes_keys_and_http_remote_private_is_denied(self):
        with patch.dict(os.environ,ENV):
            app=create_app(warmup=False)
        with TestClient(app) as client:
            status=client.get('/api/okx/demo/status')
            self.assertEqual(status.status_code,200)
            self.assertTrue(status.json()['spotTrading'])
            self.assertNotIn(ENV['OKX_DEMO_API_KEY'],status.text)
            for method,path,body in [('get','overview',None),('post','order',ORDER),('post','cancel',{'instId':'BTC-USDT','ordId':'123'})]:
                response=client.request(method,'/api/okx/demo/'+path,json=body,headers={'Authorization':'Bearer '+ENV['REBS_DEMO_ACCESS_TOKEN'],'X-Forwarded-Proto':'https'})
                self.assertEqual(response.status_code,403)
                self.assertEqual(response.headers['cache-control'],'no-store')

    def test_https_and_local_access_require_token_and_bodies_reject_live_mode(self):
        with patch.dict(os.environ,ENV): app=create_app(warmup=False)
        app.state.demo.overview=AsyncMock(return_value={'environment':'demo'})
        with TestClient(app,base_url='https://testserver') as client:
            self.assertEqual(client.get('/api/okx/demo/overview').status_code,401)
            headers={'Authorization':'Bearer '+ENV['REBS_DEMO_ACCESS_TOKEN']}
            response=client.get('/api/okx/demo/overview',headers=headers)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['cache-control'],'no-store')
            for altered in [{'environment':'live'},{'tdMode':'cross'},{'instId':'BTC-USDT-SWAP'},{'sz':'NaN'}]:
                response=client.post('/api/okx/demo/order',headers=headers,json={**ORDER,**altered})
                self.assertEqual(response.status_code,422)
        with TestClient(app,client=('127.0.0.1',32100)) as client:
            self.assertEqual(client.get('/api/okx/demo/overview',headers=headers).status_code,200)


if __name__=='__main__': unittest.main()
