"""REST pool recovery, using fake TLS streams and no external network."""
import asyncio
import os
import time
import unittest
from unittest.mock import patch

import httpcore
from httpcore._backends.base import AsyncNetworkBackend, AsyncNetworkStream
import httpx

from backend.demo import DemoService, DemoUncertain
from backend.okx import OKXService
from backend.service import UpstreamError


def success():
    return httpx.Response(200, json={'code': '0', 'data': []})


class FakeTLSStream(AsyncNetworkStream):
    def __init__(self, backend):
        self.backend = backend
        self.closed = False
        self.tls = False

    async def read(self, max_bytes, timeout=None):
        if self.tls:
            payload = b'{"code":"0","data":[]}'
            return b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload
        return b'HTTP/1.1 200 Connection Established\r\n\r\n'

    async def write(self, buffer, timeout=None):
        pass

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        if self.backend.failures:
            self.backend.failures -= 1
            await self.aclose()
            raise httpcore.ConnectError('injected TLS failure after CONNECT')
        self.tls = True
        return self

    def get_extra_info(self, info):
        return self.closed if info == 'is_readable' else None


class FakeTLSBackend(AsyncNetworkBackend):
    def __init__(self, failures=2):
        self.failures = failures
        self.streams = []

    async def connect_tcp(self, *args, **kwargs):
        stream = FakeTLSStream(self)
        self.streams.append(stream)
        return stream


class PoolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'OKX_REST_URL': 'https://openapi.okx.com', 'OKX_PROXY_URL': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_real_httpcore_tls_slot_exhaustion_recovers_on_next_get_attempt(self):
        backend = FakeTLSBackend()
        clients = []

        def factory():
            transport = httpx.AsyncHTTPTransport(proxy='http://fake-proxy.invalid:8080',
                limits=httpx.Limits(max_connections=2), trust_env=False)
            # Only the network backend is fake: exercise httpcore's actual proxy
            # CONNECT, TLS and pool bookkeeping (the 1.0.9 regression).
            transport._pool._network_backend = backend
            client = httpx.AsyncClient(base_url='https://openapi.okx.com', transport=transport,
                                       timeout=httpx.Timeout(.1, pool=.02), trust_env=False)
            clients.append(client)
            return client

        poisoned = factory()
        service = OKXService(poisoned, client_factory=factory)
        self.addAsyncCleanup(service.close)
        for _ in range(2):
            with self.assertRaises(httpx.ConnectError):
                await poisoned.get('/api/v5/public/time')
        try:
            await poisoned.get('/api/v5/public/time')
        except httpx.PoolTimeout:
            pass
        else:
            self.skipTest('Installed httpcore already reclaims failed proxy TLS slots')

        with self.assertLogs('backend.okx', level='WARNING') as logged:
            self.assertEqual(await service.get('/api/v5/public/time', {}), [])
        self.assertIn('PoolTimeout', '\n'.join(logged.output))
        self.assertEqual(len(clients), 2)
        self.assertTrue(poisoned.is_closed)
        self.assertEqual(len(backend.streams), 3)
        await service.close()
        self.assertTrue(all(client.is_closed for client in clients))
        self.assertTrue(all(stream.closed for stream in backend.streams))

    async def test_tls_failure_retires_pool_before_get_retry(self):
        clients = []
        hosts = []

        def factory():
            generation = len(clients)
            def handle(request):
                hosts.append(request.url.host)
                if not generation:
                    raise httpx.ConnectError('TLS failed', request=request)
                return success()
            client = httpx.AsyncClient(base_url='https://openapi.okx.com', transport=httpx.MockTransport(handle))
            clients.append(client)
            return client

        service = OKXService(client_factory=factory)
        self.addAsyncCleanup(service.close)
        self.assertEqual(await service.get('/api/v5/market/candles', {}), [])
        self.assertEqual(hosts, ['openapi.okx.com', 'www.okx.com'])
        self.assertEqual(len(clients), 2)
        self.assertTrue(clients[0].is_closed)
        self.assertFalse(clients[1].is_closed)

    async def test_pool_timeout_recovery_does_not_depend_on_httpcore_bug(self):
        clients, requests = [], []
        def factory():
            generation = len(clients)
            def handle(request):
                requests.append((generation, request.method))
                if generation == 0:
                    raise httpx.PoolTimeout('injected exhausted pool', request=request)
                return success()
            client = httpx.AsyncClient(base_url='https://openapi.okx.com', transport=httpx.MockTransport(handle))
            clients.append(client)
            return client
        service = OKXService(client_factory=factory)
        self.addAsyncCleanup(service.close)
        self.assertEqual(await service.get('/api/v5/market/tickers', {}), [])
        self.assertEqual(requests, [(0, 'GET'), (1, 'GET')])
        self.assertTrue(clients[0].is_closed)
        self.assertFalse(clients[1].is_closed)

    async def test_cancelled_request_retires_pool_without_replay_or_leak(self):
        clients, requests = [], []
        started = asyncio.Event()
        def factory():
            generation = len(clients)
            async def handle(request):
                requests.append((generation, request.method))
                if generation == 0:
                    started.set()
                    await asyncio.Event().wait()
                return success()
            client = httpx.AsyncClient(base_url='https://openapi.okx.com', transport=httpx.MockTransport(handle))
            clients.append(client)
            return client
        service = OKXService(client_factory=factory)
        self.addAsyncCleanup(service.close)
        pending = asyncio.create_task(service.request('POST', '/api/v5/trade/order'))
        try:
            await asyncio.wait_for(started.wait(), 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertEqual(requests, [(0, 'POST')])
            self.assertTrue(clients[0].is_closed)
            self.assertEqual(len(service._clients), 0)
            self.assertEqual(await service.get('/api/v5/market/tickers', {}), [])
            self.assertEqual(requests, [(0, 'POST'), (1, 'GET')])
            await service.close()
            self.assertTrue(all(client.is_closed for client in clients))
            self.assertEqual(len(service._clients), 0)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_concurrent_failure_does_not_close_post_or_retire_new_pool(self):
        clients = []
        slow_started, late_started = asyncio.Event(), asyncio.Event()
        finish_slow, fail_late = asyncio.Event(), asyncio.Event()
        requests = []

        def factory():
            generation = len(clients)
            async def handle(request):
                requests.append((generation, request.method, request.url.path))
                if generation == 0:
                    if request.url.path == '/slow':
                        slow_started.set()
                        await finish_slow.wait()
                        self.assertFalse(clients[0].is_closed)
                    elif request.url.path == '/late':
                        late_started.set()
                        await fail_late.wait()
                        raise httpx.ConnectError('late old-pool error')
                    else:
                        raise httpx.ConnectError('first old-pool error')
                return success()
            client = httpx.AsyncClient(base_url='https://openapi.okx.com', transport=httpx.MockTransport(handle))
            clients.append(client)
            return client

        service = OKXService(client_factory=factory)
        self.addAsyncCleanup(service.close)
        slow = asyncio.create_task(service.request('POST', '/slow'))
        late = asyncio.create_task(service.request('GET', '/late'))
        try:
            await asyncio.wait_for(slow_started.wait(), 1)
            await asyncio.wait_for(late_started.wait(), 1)
            with self.assertRaises(httpx.ConnectError):
                await service.request('GET', '/fail')
            self.assertFalse(clients[0].is_closed)
            await service.request('GET', '/fresh')
            fail_late.set()
            with self.assertRaises(httpx.ConnectError):
                await late
            await service.request('GET', '/fresh-again')
            self.assertEqual(len(clients), 2)
            self.assertFalse(clients[0].is_closed)
            finish_slow.set()
            self.assertEqual((await slow).status_code, 200)
            self.assertTrue(clients[0].is_closed)
            self.assertFalse(clients[1].is_closed)
            self.assertEqual(len([r for r in requests if r[1] == 'POST']), 1)
        finally:
            finish_slow.set()
            fail_late.set()
            await asyncio.gather(slow, late, return_exceptions=True)

    async def test_shutdown_waits_for_active_request_and_closes_all_clients(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def handle(request):
            started.set()
            await release.wait()
            self.assertFalse(client.is_closed)
            return success()
        client = httpx.AsyncClient(base_url='https://openapi.okx.com', transport=httpx.MockTransport(handle))
        service = OKXService(client)
        request = asyncio.create_task(service.request('POST', '/api/v5/trade/order'))
        await started.wait()
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        with self.assertRaisesRegex(RuntimeError, 'closing'):
            await service.request('GET', '/api/v5/public/time')
        release.set()
        await request
        await closing
        self.assertTrue(client.is_closed)
        self.assertEqual(len(service._clients), 0)

    async def test_injected_client_never_falls_back_to_real_network_and_get_is_bounded(self):
        requests = []
        def handle(request):
            requests.append(request)
            raise httpx.ConnectTimeout('offline only')
        service = OKXService(httpx.AsyncClient(base_url='https://openapi.okx.com', transport=httpx.MockTransport(handle)))
        self.addAsyncCleanup(service.close)
        with patch('backend.okx.httpx.AsyncClient', side_effect=AssertionError('real-network replacement')):
            with self.assertRaises(UpstreamError):
                await service.get('/api/v5/market/tickers', {})
        self.assertEqual(len(requests), 2)

    async def test_demo_post_is_never_replayed_and_logs_do_not_expose_secrets(self):
        clients, requests = [], []
        secret = 'sensitive-value-must-not-appear'
        def factory():
            generation = len(clients)
            def handle(request):
                requests.append(request)
                if generation == 0:
                    raise httpx.ReadTimeout(secret, request=request)
                return success()
            client = httpx.AsyncClient(base_url='https://openapi.okx.com', transport=httpx.MockTransport(handle))
            clients.append(client)
            return client
        service = OKXService(client_factory=factory)
        self.addAsyncCleanup(service.close)
        with patch.dict(os.environ, {'OKX_DEMO_API_KEY': secret, 'OKX_DEMO_SECRET_KEY': secret, 'OKX_DEMO_PASSPHRASE': secret}):
            demo = DemoService(service)
        demo.clock_synced = time.monotonic()
        with self.assertLogs('backend.okx', level='WARNING') as logged:
            with self.assertRaises(DemoUncertain) as caught:
                await demo.request('POST', '/api/v5/trade/order', body={'clOrdId': secret})
        self.assertNotIn(secret, str(caught.exception))
        output = '\n'.join(logged.output)
        self.assertIn('ReadTimeout', output)
        self.assertIn('path=/api/v5/trade/order', output)
        self.assertIn('attempt=1', output)
        self.assertNotIn(secret, output)
        self.assertNotIn(requests[0].headers['OK-ACCESS-SIGN'], output)
        self.assertEqual(len(requests), 1)
        self.assertTrue(clients[0].is_closed)
        self.assertEqual(await demo.read('/api/v5/account/balance'), [])
        self.assertEqual(len(clients), 2)
        self.assertEqual([request.method for request in requests], ['POST', 'GET'])
        self.assertEqual(requests[-1].headers['x-simulated-trading'], '1')

    async def test_public_exchange_errors_and_malformed_responses_are_sanitized(self):
        secret = 'not-a-valid-code-private-value'
        for payload in [{'code': secret, 'msg': secret, 'data': []}, ['malformed', secret]]:
            with self.subTest(payload_type=type(payload).__name__):
                client = httpx.AsyncClient(base_url='https://openapi.okx.com',
                    transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
                service = OKXService(client)
                try:
                    with self.assertLogs('backend.okx', level='WARNING') as logged:
                        with self.assertRaises(UpstreamError) as caught:
                            await service.get('/api/v5/market/tickers', {'query': secret})
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertNotIn(secret, '\n'.join(logged.output))
                finally:
                    await service.close()


if __name__ == '__main__':
    unittest.main()
