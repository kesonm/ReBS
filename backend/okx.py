"""Read-only OKX public market data. No API keys or trading endpoints."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import json
import logging
import math
import os
import re
import time
from typing import Callable
from urllib.request import getproxies
from urllib.parse import urljoin, urlsplit

import httpx
from websockets.asyncio.client import connect

from .service import InputError, UpstreamError

PERIODS = {"1s": ("second", 1), "1m": ("minute", 1), "3m": ("minute", 3),
           "5m": ("minute", 5), "15m": ("minute", 15), "30m": ("minute", 30),
           "1H": ("hour", 1), "2H": ("hour", 2), "4H": ("hour", 4),
           "1D": ("day", 1), "1W": ("week", 1), "1M": ("month", 1)}
SOURCE = "OKX · 欧易公开行情"
OKX_PROXY_HOSTS = frozenset({'openapi.okx.com', 'www.okx.com', 'ws.okx.com'})
logger = logging.getLogger(__name__)


@dataclass(eq=False)
class _ClientState:
    client: httpx.AsyncClient
    active: int = 0
    retired: bool = False
    idle: asyncio.Event = field(default_factory=asyncio.Event)
    close_task: asyncio.Task | None = None


def instrument_id(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,24}-[A-Z0-9]{1,12}", value):
        raise InputError("请输入现货币对，例如 BTC-USDT。")
    return value


def period_id(value: str) -> str:
    if value not in PERIODS:
        raise InputError("不支持的 K 线周期。")
    return value


def number(value, default: float = 0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def precision(value: str) -> int:
    try:
        return max(0, min(12, -Decimal(value).normalize().as_tuple().exponent))
    except (InvalidOperation, TypeError):
        return 2


def candle(row: list) -> dict:
    if len(row) < 9:
        raise ValueError("Invalid OKX candle")
    result = dict(zip(("timestamp", "open", "high", "low", "close", "volume"),
                      (int(row[0]), *(number(v, math.nan) for v in row[1:6]))))
    result.update(amount=number(row[7], math.nan), confirmed=str(row[8]) == "1")
    if not all(math.isfinite(result[k]) for k in ("open", "high", "low", "close", "volume", "amount")):
        raise ValueError("Non-finite OKX candle")
    if min(result[k] for k in ("open", "high", "low", "close")) <= 0 or min(result['volume'], result['amount']) < 0:
        raise ValueError("Invalid OKX price/volume")
    if result['high'] < max(result['open'], result['close'], result['low']) or result['low'] > min(result['open'], result['close']):
        raise ValueError("Invalid OKX OHLC")
    return result


def ticker(row: dict) -> dict:
    last, opening = number(row.get('last')), number(row.get('open24h'))
    return {"instId": row['instId'], "last": last, "open24h": opening,
            "change24h": (last / opening - 1) * 100 if opening > 0 else None,
            "high24h": number(row.get('high24h')), "low24h": number(row.get('low24h')),
            "volume24h": number(row.get('vol24h')), "quoteVolume24h": number(row.get('volCcy24h')),
            "bid": number(row.get('bidPx')), "ask": number(row.get('askPx')),
            "ts": int(row.get('ts') or 0)}


def trade(row: dict) -> dict:
    return {"id": str(row['tradeId']), "price": number(row['px']), "size": number(row['sz']),
            "side": row['side'], "ts": int(row['ts'])}


class OrderBook:
    """Apply full snapshot then contiguous increments; rebuild on a missing sequence."""
    def __init__(self):
        self.asks: dict[str, str] = {}
        self.bids: dict[str, str] = {}
        self.sequence: int | None = None
        self.timestamp = 0

    def apply(self, action: str, row: dict) -> None:
        seq, previous = int(row['seqId']), int(row['prevSeqId'])
        if action == 'snapshot':
            self.asks.clear()
            self.bids.clear()
        elif self.sequence is None or previous != self.sequence:
            raise ValueError('Order book sequence gap')
        for side, target in (('asks', self.asks), ('bids', self.bids)):
            for level in row.get(side, []):
                price, size = str(level[0]), str(level[1])
                if Decimal(size) == 0:
                    target.pop(price, None)
                else:
                    target[price] = size
        self.sequence = seq
        self.timestamp = int(row['ts'])

    def snapshot(self, limit: int = 400) -> dict:
        return {"asks": [[float(p), float(s)] for p, s in sorted(self.asks.items(), key=lambda pair: Decimal(pair[0]))[:limit]],
                "bids": [[float(p), float(s)] for p, s in sorted(self.bids.items(), key=lambda pair: Decimal(pair[0]), reverse=True)[:limit]],
                "ts": self.timestamp, "sequence": self.sequence, "depth": 400}


class OKXService:
    def __init__(self, client: httpx.AsyncClient | None = None, *,
                 client_factory: Callable[[], httpx.AsyncClient] | None = None):
        self.base_url = os.getenv('OKX_REST_URL', 'https://openapi.okx.com').rstrip('/')
        self.ws_base = os.getenv('OKX_WS_URL', 'wss://ws.okx.com/ws/v5').rstrip('/')
        self.proxy_url = os.getenv('OKX_PROXY_URL') or None
        # An injected client has no implicit production-network replacement.
        self._client_factory = client_factory or (self._new_client if client is None else None)
        self._current = _ClientState(client) if client is not None else None
        self._clients = {self._current} if self._current else set()
        self._closing = False
        self.cache: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self.inflight: dict[tuple, asyncio.Task] = {}

    async def start(self):
        self._closing = False
        self._ready_client()

    @property
    def client(self) -> httpx.AsyncClient | None:
        return self._current.client if self._current is not None else None

    def _new_client(self) -> httpx.AsyncClient:
        if self.proxy_url:
            self.proxy_for(self.base_url)
        return httpx.AsyncClient(base_url=self.base_url,
            timeout=httpx.Timeout(10, connect=6), limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=15),
            headers={'User-Agent': 'ReBS-Market/2.0', 'Accept': 'application/json'},
            proxy=self.proxy_url, trust_env=self.proxy_url is None)

    def _ready_client(self) -> _ClientState:
        if self._closing:
            raise RuntimeError('OKX service is closing')
        if self._current is None:
            if self._client_factory is None:
                raise RuntimeError('No OKX client factory configured')
            self._current = _ClientState(self._client_factory())
            self._clients.add(self._current)
        return self._current

    def _retire_client(self, state: _ClientState):
        # No await between identity check and retirement: concurrent failures on
        # an old pool must not retire a replacement already serving new requests.
        if self._client_factory is not None and self._current is state:
            self._current = None
            state.retired = True

    def _close_client(self, state: _ClientState) -> asyncio.Task:
        if state.close_task is None:
            async def close_idle():
                try:
                    if state.active:
                        await state.idle.wait()
                    await state.client.aclose()
                except Exception as exc:
                    logger.warning('OKX REST client close failed error=%s', type(exc).__name__)
                finally:
                    self._clients.discard(state)
            state.close_task = asyncio.create_task(close_idle())
        return state.close_task

    def log_rest_failure(self, method: str, target: str, attempt: int,
                         error: str, status: int | None = None):
        url = urlsplit(urljoin(self.base_url + '/', target))
        # Never log query strings, bodies, headers or exception messages; private
        # requests use this same transport and may contain signed credentials.
        logger.warning('OKX REST failure method=%s host=%s path=%s attempt=%s error=%s status=%s',
                       method, url.hostname, url.path, attempt, error, status)

    async def request(self, method: str, target: str, *, attempt: int = 1, **kwargs) -> httpx.Response:
        """Send exactly once; retire failed pools without interrupting other calls."""
        if self.proxy_url:
            self.proxy_for(urljoin(self.base_url + '/', target))
        state = self._ready_client()
        state.active += 1
        state.idle.clear()
        try:
            response = await state.client.request(method, target, **kwargs)
            if response.is_error:
                self.log_rest_failure(method, target, attempt, 'HTTPStatusError', response.status_code)
            return response
        except httpx.TransportError as exc:
            self.log_rest_failure(method, target, attempt, type(exc).__name__)
            self._retire_client(state)
            raise
        except asyncio.CancelledError:
            # Cancellation during proxy TLS negotiation can leave the same pool
            # accounting defect as a failed handshake.
            self._retire_client(state)
            raise
        finally:
            state.active -= 1
            if not state.active:
                state.idle.set()
                if state.retired:
                    await asyncio.shield(self._close_client(state))

    def proxy_for(self, url: str) -> str | None:
        if self.proxy_url:
            target = urlsplit(url)
            if target.scheme not in {'https', 'wss'} or target.hostname not in OKX_PROXY_HOSTS or target.port not in {None, 443}:
                raise InputError('项目专用代理仅允许欧易公开行情域名和标准 TLS 端口。')
            return self.proxy_url
        return getproxies().get('https')

    async def close(self):
        self._closing = True
        tasks = list(self.inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        states = list(self._clients)
        self._current = None
        for state in states:
            state.retired = True
        await asyncio.gather(*(asyncio.shield(self._close_client(state)) for state in states))

    async def get(self, path: str, params: dict) -> list:
        for attempt in range(2):
            try:
                # Both are OKX's global public endpoints. Explicit regional/custom
                # configuration is respected and never silently moved elsewhere.
                target = 'https://www.okx.com' + path if attempt and self.base_url == 'https://openapi.okx.com' else path
                response = await self.request('GET', target, attempt=attempt + 1, params=params)
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError('Invalid response')
                if body.get('code') != '0':
                    code = str(body.get('code', 'unknown'))
                    safe_code = code if code.isdigit() and len(code) <= 12 else 'unknown'
                    self.log_rest_failure('GET', target, attempt + 1, 'ExchangeError', response.status_code)
                    raise UpstreamError(f"欧易行情返回错误（{safe_code}），请稍后重试。")
                if not isinstance(body.get('data'), list):
                    raise ValueError('Invalid response')
                return body['data']
            except (InputError, UpstreamError):
                raise
            except (httpx.HTTPError, ValueError) as exc:
                if isinstance(exc, ValueError):
                    self.log_rest_failure('GET', target, attempt + 1, 'InvalidResponse')
                retryable = isinstance(exc, httpx.TransportError) or isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code >= 500 or exc.response.status_code == 429)
                if attempt == 0 and retryable:
                    await asyncio.sleep(.35)
                    continue
                raise UpstreamError('欧易行情暂时连接失败，请检查网络后重试。') from exc
        raise UpstreamError('欧易行情暂时不可用。')

    async def cached(self, key: tuple, ttl: float, loader):
        previous = self.cache.get(key)
        if previous and time.monotonic() - previous[0] < ttl:
            self.cache.move_to_end(key)
            return previous[1]
        task = self.inflight.get(key)
        if task is None:
            async def load():
                try:
                    value = await loader()
                    self.cache[key] = (time.monotonic(), value)
                    self.cache.move_to_end(key)
                    while len(self.cache) > 128:
                        self.cache.popitem(last=False)
                    return value
                finally:
                    self.inflight.pop(key, None)
            task = asyncio.create_task(load())
            self.inflight[key] = task
        return await asyncio.shield(task)

    async def instruments(self) -> list:
        async def load():
            rows = await self.get('/api/v5/public/instruments', {'instType': 'SPOT'})
            return [{"instId": r['instId'], "base": r['baseCcy'], "quote": r['quoteCcy'],
                     "tickSize": r['tickSz'], "lotSize": r['lotSz'], "minSize": r['minSz'],
                     "pricePrecision": precision(r['tickSz']), "volumePrecision": precision(r['lotSz'])}
                    for r in rows if r.get('state') == 'live']
        return await self.cached(('instruments',), 300, load)

    async def markets(self) -> dict:
        async def load():
            instruments, rows = await asyncio.gather(self.instruments(), self.get('/api/v5/market/tickers', {'instType': 'SPOT'}))
            tickers = {r['instId']: ticker(r) for r in rows if number(r.get('last')) > 0}
            items = [{**i, **tickers[i['instId']]} for i in instruments if i['instId'] in tickers]
            items.sort(key=lambda item: item['quoteVolume24h'], reverse=True)
            return {"items": items, "source": SOURCE, "fetchedAt": int(time.time() * 1000)}
        return await self.cached(('markets',), 3, load)

    async def candles(self, inst: str, period: str, before: int | None, limit: int) -> dict:
        inst, period = instrument_id(inst), period_id(period)
        size = min(limit, 100 if before else 300)
        async def load():
            # OKX's `after` means older; the local `before` cursor is exclusive.
            params = {'instId': inst, 'bar': period, 'limit': str(size)}
            historical = before is not None or period == '1s'
            if before is not None:
                params['after'] = str(before)
            rows = await self.get('/api/v5/market/history-candles' if historical else '/api/v5/market/candles', params)
            try:
                values = {int(r[0]): candle(r) for r in rows if before is None or int(r[0]) < before}
            except (ValueError, KeyError, IndexError) as exc:
                raise UpstreamError('欧易 K 线数据格式异常，请稍后重试。') from exc
            return {"instId": inst, "period": period, "bars": [values[k] for k in sorted(values)],
                    "hasMore": len(rows) >= size, "source": SOURCE, "fetchedAt": int(time.time()*1000)}
        return await self.cached(('candles', inst, period, before, size), 1 if before is None else 60, load)

    async def snapshot(self, inst: str) -> dict:
        inst = instrument_id(inst)
        async def load():
            ticks, books, trades = await asyncio.gather(
                self.get('/api/v5/market/ticker', {'instId': inst}),
                self.get('/api/v5/market/books', {'instId': inst, 'sz': '400'}),
                self.get('/api/v5/market/trades', {'instId': inst, 'limit': '100'}))
            if not ticks or not books:
                raise UpstreamError('欧易暂未返回该币对的行情。')
            b = books[0]
            return {"instId": inst, "ticker": ticker(ticks[0]),
                    "book": {"asks": [[number(x[0]), number(x[1])] for x in b['asks']],
                             "bids": [[number(x[0]), number(x[1])] for x in b['bids']],
                             "ts": int(b['ts']), "depth": 400},
                    "trades": [trade(r) for r in trades], "tradeSource": "REST · 近期成交（可能聚合）", "source": SOURCE}
        return await self.cached(('snapshot', inst), 1, load)

    async def stream(self, socket, inst: str, period: str):
        """Two upstream sockets: public books/ticker and business candles/trades-all."""
        send_lock = asyncio.Lock()
        async def send(payload):
            async with send_lock:
                await socket.send_json(payload)

        async def relay(service: str, channels: list[str]):
            delay = 1
            while True:
                book = OrderBook()
                try:
                    await send({'type': 'status', 'stream': service, 'state': 'connecting'})
                    async with connect(f'{self.ws_base}/{service}', open_timeout=10, close_timeout=2,
                                       ping_interval=None, max_queue=32, max_size=2**22,
                                       proxy=self.proxy_for(f'{self.ws_base}/{service}')) as upstream:
                        await upstream.send(json.dumps({'op': 'subscribe', 'args': [{'channel': c, 'instId': inst} for c in channels]}))
                        subscribed = set()
                        while True:
                            try:
                                raw = await asyncio.wait_for(upstream.recv(), timeout=18)
                            except asyncio.TimeoutError:
                                await upstream.send('ping')
                                raw = await asyncio.wait_for(upstream.recv(), timeout=8)
                            if raw == 'pong':
                                await send({'type':'heartbeat','stream':service,'ts':int(time.time()*1000)})
                                continue
                            message = json.loads(raw)
                            if message.get('event') == 'error':
                                raise UpstreamError(f"频道订阅失败（{message.get('code')}）：{message.get('msg', '')}")
                            channel = message.get('arg', {}).get('channel')
                            if message.get('event') == 'subscribe':
                                subscribed.add(channel)
                                if subscribed.issuperset(channels):
                                    delay = 1
                                    await send({'type':'status','stream':service,'state':'live'})
                                continue
                            rows = message.get('data')
                            if not rows:
                                continue
                            if channel == 'books':
                                book.apply(message.get('action', ''), rows[0])
                                await send({'type':'book','data':book.snapshot()})
                            elif channel == 'tickers':
                                await send({'type':'ticker','data':ticker(rows[0])})
                            elif channel == 'trades-all':
                                await send({'type':'trades','data':[trade(r) for r in rows], 'source':'trades-all'})
                            elif channel == f'candle{period}':
                                await send({'type':'candle','data':candle(rows[0])})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    message = str(exc)[:180] if isinstance(exc, UpstreamError) else '欧易实时连接中断，正在重连。'
                    await send({'type':'status','stream':service,'state':'reconnecting','message':message})
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 15)

        async def receive():
            while True:
                await socket.receive_text()

        tasks = [asyncio.create_task(relay('public', ['tickers', 'books'])),
                 asyncio.create_task(relay('business', [f'candle{period}', 'trades-all'])),
                 asyncio.create_task(receive())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
