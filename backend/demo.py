"""Authenticated OKX demo account and cash spot orders; no live-mode switch."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from decimal import Decimal, localcontext
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import HTTPException, Request

from .okx import OKXService, instrument_id
from .service import InputError, PROJECT_ROOT

READ_ENDPOINTS = frozenset({
    '/api/v5/account/balance', '/api/v5/account/positions',
    '/api/v5/trade/orders-pending', '/api/v5/trade/orders-history', '/api/v5/trade/fills',
    '/api/v5/trade/order', '/api/v5/public/instruments',
})
WRITE_ENDPOINTS = frozenset({'/api/v5/trade/order', '/api/v5/trade/cancel-order'})


class DemoError(RuntimeError):
    pass


class DemoUncertain(DemoError):
    """A write may have reached the exchange; never automatically resend it."""


def sign(secret: str, timestamp: str, method: str, request_path: str, body: str = '') -> str:
    message = timestamp + method.upper() + request_path + body
    return base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()


def require_private_access(request: Request, token: str):
    try:
        loopback = ipaddress.ip_address(request.client.host).is_loopback if request.client else False
    except ValueError:
        loopback = False
    if request.url.scheme != 'https' and not loopback:
        raise HTTPException(403, '模拟账户请通过 HTTPS 或 SSH 本地转发访问，避免明文传输访问口令。',headers={'Cache-Control':'no-store'})
    if len(token) < 24:
        raise HTTPException(503, '请先在服务器设置模拟账户访问口令。',headers={'Cache-Control':'no-store'})
    authorization = request.headers.get('authorization', '')
    supplied = authorization[7:] if authorization.startswith('Bearer ') else ''
    if len(supplied) > 256 or not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(401, '模拟账户访问口令不正确。', headers={'WWW-Authenticate':'Bearer','Cache-Control':'no-store'})


class DemoService:
    def __init__(self, market: OKXService):
        self.market = market
        self.key = os.getenv('OKX_DEMO_API_KEY', '')
        self.secret = os.getenv('OKX_DEMO_SECRET_KEY', '')
        self.passphrase = os.getenv('OKX_DEMO_PASSPHRASE', '')
        self.access_token = os.getenv('REBS_DEMO_ACCESS_TOKEN', '')
        self.clock_lock = asyncio.Lock()
        self.clock_synced = 0.0
        self.clock_offset = 0.0
        self.ledger_path = Path(os.getenv('REBS_DEMO_STATE_PATH', str(PROJECT_ROOT / '.cache' / 'demo-orders.sqlite3')))

    @property
    def credentials_configured(self):
        return bool(self.key and self.secret and self.passphrase)

    def status(self):
        return {'environment':'demo', 'readOnly':False, 'spotTrading':True, 'credentialsConfigured':self.credentials_configured,
                'accessConfigured':len(self.access_token) >= 24, 'refreshSeconds':15}

    async def sync_clock(self, force=False):
        async with self.clock_lock:
            if not force and self.clock_synced and time.monotonic()-self.clock_synced < 300:
                return
            before = time.time()
            rows = await self.market.get('/api/v5/public/time', {})
            after = time.time()
            try:
                self.clock_offset = int(rows[0]['ts'])/1000 - (before+after)/2
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise DemoError('无法校准欧易时间，请稍后重试。') from exc
            self.clock_synced = time.monotonic()

    async def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> list:
        if not (method == 'GET' and path in READ_ENDPOINTS or method == 'POST' and path in WRITE_ENDPOINTS):
            raise DemoError('模拟账户仅开放指定接口。')
        if not self.credentials_configured:
            raise DemoError('尚未配置欧易模拟盘 API Key、Secret Key 和 Passphrase。')
        target = urlsplit(self.market.base_url)
        if target.scheme != 'https' or target.hostname not in {'openapi.okx.com', 'www.okx.com'} or target.port not in {None,443}:
            raise DemoError('模拟账户只允许连接欧易官方全球 REST 地址。')
        query = urlencode(params or {}) if method == 'GET' else ''
        request_path = path + ('?'+query if query else '')
        content = json.dumps(body,separators=(',',':'),ensure_ascii=True) if body is not None else ''
        await self.sync_clock()
        assert self.market.client is not None
        # GET can retry. A POST is sent exactly once, including clock/transport errors.
        for attempt in range(2 if method == 'GET' else 1):
            timestamp = datetime.fromtimestamp(time.time()+self.clock_offset,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
            headers = {'OK-ACCESS-KEY':self.key, 'OK-ACCESS-PASSPHRASE':self.passphrase,
                       'OK-ACCESS-TIMESTAMP':timestamp, 'OK-ACCESS-SIGN':sign(self.secret,timestamp,method,request_path,content),
                       'x-simulated-trading':'1', 'Content-Type':'application/json'}
            if method == 'POST' and path == '/api/v5/trade/order':
                headers['expTime'] = str(int((time.time()+self.clock_offset+10)*1000))
            try:
                response = await self.market.client.request(method,request_path,headers=headers,content=content or None)
                payload = response.json()
                if not isinstance(payload,dict): raise ValueError('Invalid response')
            except (httpx.HTTPError, ValueError) as exc:
                if method == 'POST':
                    raise DemoUncertain('请求结果待确认，请查询委托状态；系统不会自动重复提交。') from exc
                if not attempt:
                    await asyncio.sleep(.35)
                    continue
                raise DemoError('模拟账户连接暂时失败，请稍后重试。') from exc
            code = str(payload.get('code','unknown'))
            if method == 'POST' and not code.isdigit():
                raise DemoUncertain('委托响应异常，请查询结果。')
            if code == '50102' and not attempt and method == 'GET':
                await self.sync_clock(force=True)
                continue
            if method == 'POST' and (response.status_code >= 500 or code in {'50004','50013'}):
                raise DemoUncertain('欧易响应超时，委托结果待确认，请查询后再操作。')
            if code != '0' or response.is_error:
                safe_code = code if code.isdigit() else str(response.status_code)
                explanations = {'50101':'API Key 与模拟环境不匹配，请使用模拟盘专用密钥。',
                    '50111':'模拟盘 API Key 无效。', '50113':'签名验证失败，请检查 Secret Key。',
                    '50105':'Passphrase 不匹配。', '50110':'当前代理出口不在该 API Key 的 IP 白名单中。',
                    '50102':'服务器与欧易时间偏差过大。', '50120':'API Key 缺少所需权限。'}
                raise DemoError(f"模拟账户请求失败（{safe_code}）。"+explanations.get(code,'请检查模拟盘密钥和查询权限。'))
            if not isinstance(payload.get('data'),list):
                if method == 'POST': raise DemoUncertain('委托响应格式异常，请查询结果。')
                raise DemoError('模拟账户响应格式异常。')
            return payload['data']
        raise DemoError('模拟账户查询失败。')

    async def read(self, path: str, params: dict | None = None):
        return await self.request('GET',path,params)

    async def overview(self, fresh=False):
        async def load():
            sections = [self.read('/api/v5/account/balance'), self.read('/api/v5/account/positions'),
                self.read('/api/v5/trade/orders-pending',{'instType':'SPOT','limit':'100'}),
                self.read('/api/v5/trade/orders-history',{'instType':'SPOT','limit':'50'}),
                self.read('/api/v5/trade/fills',{'instType':'SPOT','limit':'50'})]
            balance, positions, pending, history, fills = await asyncio.gather(*sections,return_exceptions=True)
            if isinstance(balance,Exception):
                raise DemoError(str(balance) if isinstance(balance,DemoError) else '模拟账户连接失败，请稍后重试。') from balance
            account = balance[0] if balance else {}
            warnings=[]
            def rows(value, name, fields):
                if isinstance(value,Exception):
                    warnings.append({'section':name,'message':str(value) if isinstance(value,DemoError) else '该部分查询暂时失败。'})
                    return None
                return [{key:row.get(key,'') for key in fields} for row in value]
            return {'environment':'demo','readOnly':False,'fetchedAt':int(time.time()*1000),
                'accountTime':account.get('uTime',''), 'totalEquity':account.get('totalEq') or None,
                'availableEquity':account.get('availEq') or None,
                'balances':[{k:r.get(k,'') for k in ['ccy','eq','cashBal','availBal','frozenBal','eqUsd','upl']} for r in account.get('details',[])],
                'positions':rows(positions,'持仓',['instId','instType','posSide','pos','posCcy','avgPx','markPx','upl','uplRatio','lever','mgnMode','uTime']),
                'pending':rows(pending,'当前委托',['ordId','clOrdId','instId','side','ordType','px','sz','accFillSz','state','cTime','tgtCcy']),
                'history':rows(history,'历史委托',['ordId','clOrdId','instId','side','ordType','px','avgPx','sz','accFillSz','state','cTime','uTime','tgtCcy']),
                'fills':rows(fills,'成交记录',['tradeId','ordId','instId','side','fillPx','fillSz','fee','feeCcy','fillTime','ts']),
                'warnings':warnings}
        if not self.credentials_configured:
            raise DemoError('尚未配置模拟盘凭据，请先在服务器完成设置。')
        return await load() if fresh else await self.market.cached(('private-demo-overview',),3,load)

    async def instruments(self):
        async def load():
            return await self.read('/api/v5/public/instruments',{'instType':'SPOT'})
        return await self.market.cached(('demo-instruments',),300,load)

    @contextmanager
    def ledger(self):
        self.ledger_path.parent.mkdir(parents=True,exist_ok=True)
        connection = sqlite3.connect(self.ledger_path,timeout=5)
        try:
            with connection:
                connection.execute('CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT)')
                yield connection
        finally:
            connection.close()

    async def place(self, data: dict):
        inst = instrument_id(data['instId'])
        client_id = data['clOrdId']
        if not re.fullmatch(r'[a-f0-9]{32}',client_id): raise InputError('客户端委托号格式错误。')
        if data['side'] not in {'buy','sell'} or data['ordType'] not in {'limit','market'}:
            raise InputError('仅支持现货买卖及限价单、市价单。')
        body = {'instId':inst,'tdMode':'cash','side':data['side'],'ordType':data['ordType'],
                'sz':data['sz'],'clOrdId':client_id}
        if data['ordType'] == 'limit': body['px'] = data.get('px') or ''
        else: body.update(tgtCcy='base_ccy',banAmend=True)
        fingerprint = hashlib.sha256((self.key+json.dumps(body,sort_keys=True)).encode()).hexdigest()
        # Persist before the network write. Retrying the same ID, including after a
        # process restart, only returns its acknowledgement or queries OKX.
        with self.ledger() as db:
            previous = db.execute('SELECT fingerprint,result FROM submissions WHERE id=?',(client_id,)).fetchone()
        if previous:
            if previous[0] != fingerprint: raise InputError('该委托号已用于其他参数，请使用新的委托号。')
            if previous[1]: return json.loads(previous[1])
            return await self.resolve(inst,client_id)
        info = next((row for row in await self.instruments() if row.get('instId') == inst and row.get('state') == 'live'),None)
        if info is None: raise InputError('该币对暂不支持模拟现货交易。')
        with localcontext() as context:
            context.prec = 100
            for field,step,minimum,label in [('sz',info['lotSz'],info['minSz'],'数量'), *([('px',info['tickSz'],info['tickSz'],'价格')] if data['ordType']=='limit' else [])]:
                value = body[field]
                if not isinstance(value,str) or len(value)>48 or not re.fullmatch(r'\d+(\.\d+)?',value):
                    raise InputError(f'{label}必须是正数，不支持科学计数法。')
                decimal = Decimal(value)
                if decimal <= 0 or decimal < Decimal(minimum) or decimal % Decimal(step) != 0:
                    raise InputError(f'{label}至少为 {minimum}，且须符合步长 {step}。')
        await self.sync_clock()
        with self.ledger() as db:
            cursor = db.execute('INSERT OR IGNORE INTO submissions (id,fingerprint) VALUES (?,?)',(client_id,fingerprint))
            inserted = cursor.rowcount == 1
        if not inserted: return await self.place(data)
        try:
            result = self.ack(await self.request('POST','/api/v5/trade/order',body=body),inst,client_id)
        except DemoUncertain:
            return await self.resolve(inst,client_id)
        except DemoError as exc:
            result = {'environment':'demo','outcome':'rejected','message':str(exc),'instId':inst,'clOrdId':client_id}
        with self.ledger() as db:
            db.execute('UPDATE submissions SET result=? WHERE id=?',(json.dumps(result),client_id))
        self.market.cache.pop(('private-demo-overview',),None)
        return result

    @staticmethod
    def ack(rows, inst, client_id=''):
        if not rows or not isinstance(rows[0],dict) or 'sCode' not in rows[0]:
            raise DemoUncertain('未收到有效委托回执，请查询结果。')
        row = rows[0]
        code = str(row['sCode'])
        if code in {'50004','50013'}:
            raise DemoUncertain('欧易委托回执暂未确认，请查询结果。')
        if code != '0':
            messages = {'51008':'模拟账户可用余额不足。','51000':'订单参数不符合账户要求。',
                '51001':'该币对不可用。','51006':'价格超出欧易允许范围。',
                '51020':'委托金额低于最小要求。','51603':'委托不存在或已结束。','51604':'委托已撤销。',
                '51010':'账户模式不支持该交易，请在欧易模拟盘调整账户模式。'}
            safe_code = code if code.isdigit() else 'unknown'
            raise DemoError(f'模拟委托失败（{safe_code}）。'+messages.get(code,'请核对模拟余额、委托参数及 API 交易权限。'))
        return {'environment':'demo','outcome':'accepted','instId':inst,'clOrdId':client_id,
                'ordId':str(row.get('ordId','')),'message':'请求已受理，成交或撤单结果以委托状态为准。'}

    async def resolve(self, inst: str, client_id: str):
        try:
            rows = await self.read('/api/v5/trade/order',{'instId':instrument_id(inst),'clOrdId':client_id})
            if rows:
                row=rows[0]
                return {'environment':'demo','outcome':'confirmed','instId':inst,'clOrdId':client_id,
                        'ordId':row.get('ordId',''),'state':row.get('state',''),'message':'已查到模拟委托，请查看最新状态。'}
        except DemoError: pass
        return {'environment':'demo','outcome':'unknown','instId':inst,'clOrdId':client_id,
                'message':'委托结果待确认，系统不会重复下单。请查询委托或到欧易模拟盘核对。'}

    async def cancel(self, inst: str, order_id: str):
        inst=instrument_id(inst)
        try:
            rows=await self.request('POST','/api/v5/trade/cancel-order',body={'instId':inst,'ordId':order_id})
            result=self.ack(rows,inst)
        except DemoUncertain:
            return {'environment':'demo','outcome':'unknown','instId':inst,'ordId':order_id,
                    'message':'撤单结果待确认，请刷新委托状态；系统不会自动重发。'}
        self.market.cache.pop(('private-demo-overview',),None)
        return result
