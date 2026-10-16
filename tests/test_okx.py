import asyncio
import unittest
import os
from unittest.mock import patch, AsyncMock

import httpx
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.okx import OKXService, OrderBook, candle, instrument_id, period_id, precision, ticker
from backend.service import InputError, UpstreamError


class NormalizationTests(unittest.TestCase):
    def test_spot_volume_quote_amount_and_unconfirmed_candle(self):
        bar = candle(['1700000000000','100','105','95','102','1.25','127.5','127.5','0'])
        self.assertEqual(bar['volume'],1.25)
        self.assertEqual(bar['amount'],127.5)
        self.assertEqual(bar['timestamp'],1700000000000)
        self.assertFalse(bar['confirmed'])

    def test_invalid_ohlc_and_nan_are_rejected(self):
        for row in [['1','100','90','95','102','1','1','1','1'],['1','100','105','95','nan','1','1','1','1']]:
            with self.assertRaises(ValueError): candle(row)

    def test_precision_comes_from_instrument_tick(self):
        self.assertEqual(precision('0.000000010000'),8)
        self.assertEqual(precision('10'),0)
        self.assertEqual(precision('0.1'),1)

    def test_spot_only_and_period_validation(self):
        self.assertEqual(instrument_id('btc-usdt'),'BTC-USDT')
        self.assertEqual(period_id('1s'),'1s')
        for invalid in ['BTC-USDT-SWAP','../../BTC','BTC/USDT']:
            with self.assertRaises(InputError): instrument_id(invalid)
        with self.assertRaises(InputError): period_id('2s')

    def test_24h_change_uses_rolling_open(self):
        data=ticker({'instId':'BTC-USDT','last':'105','open24h':'100','ts':'1700'})
        self.assertAlmostEqual(data['change24h'],5)
        self.assertEqual(data['ts'],1700)

    def test_orderbook_increment_delete_sort_and_sequence_gap(self):
        book=OrderBook()
        book.apply('snapshot',{'seqId':10,'prevSeqId':-1,'ts':'1000','asks':[['103','1'],['102','2']],'bids':[['99','3'],['100','4']]})
        book.apply('update',{'seqId':13,'prevSeqId':10,'ts':'1010','asks':[['102','0'],['101','5']],'bids':[['100','2']]})
        result=book.snapshot()
        self.assertEqual(result['asks'],[[101,5],[103,1]])
        self.assertEqual(result['bids'],[[100,2],[99,3]])
        self.assertEqual(result['sequence'],13)
        with self.assertRaises(ValueError):
            book.apply('update',{'seqId':20,'prevSeqId':18,'ts':'1020'})
        book.apply('snapshot',{'seqId':21,'prevSeqId':-1,'ts':'1030','asks':[['103','1']],'bids':[]})
        self.assertEqual(book.snapshot()['bids'],[])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests=[]
        async def handle(request):
            self.requests.append(request)
            return self.handler(request)
        self.service=OKXService(httpx.AsyncClient(base_url='https://test.okx.com',transport=httpx.MockTransport(handle)))

    async def asyncTearDown(self):
        await self.service.close()

    async def test_history_cursor_uses_after_and_excludes_boundary(self):
        def response(request):
            self.assertEqual(request.url.path,'/api/v5/market/history-candles')
            self.assertEqual(request.url.params['after'],'3000')
            self.assertEqual(request.url.params['limit'],'100')
            row=lambda ts:[str(ts),'1','2','1','2','1','2','2','1']
            return httpx.Response(200,json={'code':'0','data':[row(3000),row(2000),row(1000),row(2000)]})
        self.handler=response
        data=await self.service.candles('BTC-USDT','15m',3000,300)
        self.assertEqual([b['timestamp'] for b in data['bars']],[1000,2000])

    async def test_one_second_uses_history_endpoint(self):
        def response(request):
            self.assertEqual(request.url.path,'/api/v5/market/history-candles')
            return httpx.Response(200,json={'code':'0','data':[]})
        self.handler=response
        result=await self.service.candles('BTC-USDT','1s',None,300)
        self.assertFalse(result['hasMore'])

    async def test_exchange_error_is_not_cached_as_empty_success(self):
        self.handler=lambda request:httpx.Response(200,json={'code':'51001','msg':'Instrument does not exist','data':[]})
        with self.assertRaises(UpstreamError): await self.service.candles('ZZZ-USDT','15m',None,300)
        self.assertEqual(len(self.service.cache),0)

    async def test_http_failure_is_actionable(self):
        self.handler=lambda request:httpx.Response(429,text='Rate limited')
        with self.assertRaisesRegex(UpstreamError,'欧易行情暂时连接失败'):
            await self.service.candles('BTC-USDT','15m',None,300)

    async def test_transient_connection_failure_retries_public_global_endpoint(self):
        calls=0
        def response(request):
            nonlocal calls
            calls+=1
            if calls==1:
                raise httpx.ConnectTimeout('Temporary network issue',request=request)
            self.assertEqual(request.url.host,'www.okx.com')
            return httpx.Response(200,json={'code':'0','data':[]})
        self.handler=response
        result=await self.service.candles('BTC-USDT','15m',None,300)
        self.assertEqual(calls,2)
        self.assertEqual(result['bars'],[])

    async def test_rest_bootstrap_trade_units_and_depth_are_preserved(self):
        def response(request):
            if request.url.path.endswith('/ticker'):
                rows=[{'instId':'BTC-USDT','last':'101','open24h':'100','ts':'1234'}]
            elif request.url.path.endswith('/books'):
                self.assertEqual(request.url.params['sz'],'400')
                rows=[{'asks':[['102','0.001','0','1']],'bids':[['100','0.002','0','1']],'ts':'1234'}]
            else: rows=[{'tradeId':'99','px':'101','sz':'0.00000123','side':'buy','ts':'1234'}]
            return httpx.Response(200,json={'code':'0','data':rows})
        self.handler=response
        result=await self.service.snapshot('BTC-USDT')
        self.assertEqual(result['book']['asks'],[[102,0.001]])
        self.assertEqual(result['trades'][0]['size'],0.00000123)
        self.assertEqual(result['book']['depth'],400)

    async def test_concurrent_identical_queries_share_upstream_and_cache_isolated(self):
        self.handler=lambda request:httpx.Response(200,json={'code':'0','data':[]})
        await asyncio.gather(*(self.service.candles('BTC-USDT','15m',None,300) for _ in range(5)))
        self.assertEqual(len(self.requests),1)
        await self.service.candles('ETH-USDT','15m',None,300)
        await self.service.candles('BTC-USDT','1H',None,300)
        self.assertEqual(len(self.requests),3)

    async def test_live_instruments_and_tickers_merge_with_correct_precision(self):
        def response(request):
            if request.url.path.endswith('instruments'):
                rows=[{'instId':'BTC-USDT','baseCcy':'BTC','quoteCcy':'USDT','tickSz':'0.1','lotSz':'0.000001','minSz':'0.00001','state':'live'},
                      {'instId':'OLD-USDT','state':'suspend'}]
            else: rows=[{'instId':'BTC-USDT','last':'102','open24h':'100','vol24h':'2','volCcy24h':'204','ts':'1700'}]
            return httpx.Response(200,json={'code':'0','data':rows})
        self.handler=response
        result=await self.service.markets()
        self.assertEqual(len(result['items']),1)
        self.assertEqual(result['items'][0]['pricePrecision'],1)
        self.assertEqual(result['items'][0]['quoteVolume24h'],204)

    async def test_scoped_proxy_rejects_other_hosts_and_does_not_change_global_environment(self):
        with patch.dict(os.environ, {'OKX_PROXY_URL':'http://127.0.0.1:18081', 'HTTPS_PROXY':'http://existing-proxy:7890'}):
            service=OKXService(self.service.client)
            self.assertEqual(service.proxy_for('https://openapi.okx.com/api/v5/public/time'),'http://127.0.0.1:18081')
            self.assertEqual(service.proxy_for('wss://ws.okx.com/ws/v5/public'),'http://127.0.0.1:18081')
            for url in ['https://pypi.org/', 'https://www.okx.com.evil.example/', 'http://www.okx.com/', 'https://ws.okx.com:8443/']:
                with self.assertRaises(InputError): service.proxy_for(url)
            self.assertEqual(os.environ['HTTPS_PROXY'],'http://existing-proxy:7890')
            with self.assertRaises(InputError): await service.get('https://pypi.org/',{})
            self.assertEqual(len(self.requests),0)

    async def test_explicit_okx_proxy_disables_ambient_proxy_for_dedicated_http_client(self):
        with patch.dict(os.environ, {'OKX_PROXY_URL':'http://127.0.0.1:18081'}):
            service=OKXService()
            with patch('backend.okx.httpx.AsyncClient',return_value=AsyncMock()) as client:
                await service.start()
                self.assertEqual(client.call_args.kwargs['proxy'],'http://127.0.0.1:18081')
                self.assertFalse(client.call_args.kwargs['trust_env'])
                await service.close()


class APIValidationTests(unittest.TestCase):
    def test_local_health_invalid_parameters_and_unknown_api(self):
        with TestClient(create_app(warmup=False)) as client:
            self.assertEqual(client.get('/api/health').json()['source'],'OKX')
            self.assertEqual(client.get('/api/okx/candles?instId=BTC-USDT-SWAP').status_code,400)
            self.assertEqual(client.get('/api/okx/candles?period=2s').status_code,400)
            self.assertEqual(client.get('/api/okx/candles?before=-1').status_code,422)
            self.assertEqual(client.get('/api/okx/missing').status_code,404)
            with client.websocket_connect('/api/okx/stream?instId=BTC-USDT-SWAP') as ws:
                self.assertEqual(ws.receive_json()['type'],'error')


if __name__ == '__main__': unittest.main()
