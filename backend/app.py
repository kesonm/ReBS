"""FastAPI entry point; serves the built Vue application when available."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .service import DataError, InputError, MarketService, PROJECT_ROOT, UpstreamError
from .okx import OKXService, instrument_id, period_id
from .demo import DemoService, DemoError, require_private_access
from .strategy import create_router as create_strategy_router


class DemoOrder(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instId: str = Field(pattern=r'^[A-Z0-9]{1,24}-[A-Z0-9]{1,12}$',max_length=37)
    clOrdId: str = Field(pattern=r'^[a-f0-9]{32}$')
    side: Literal['buy','sell']
    ordType: Literal['limit','market']
    sz: str = Field(min_length=1,max_length=48,pattern=r'^\d+(\.\d+)?$')
    px: str | None = Field(default=None,max_length=48,pattern=r'^\d+(\.\d+)?$')


class DemoCancel(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instId: str = Field(pattern=r'^[A-Z0-9]{1,24}-[A-Z0-9]{1,12}$',max_length=37)
    ordId: str = Field(pattern=r'^\d{1,32}$')


def create_app(service: MarketService | None = None, *, warmup: bool = True, dist_dir: Path | None = None) -> FastAPI:
    market = service or MarketService()
    okx = OKXService()
    demo = DemoService(okx)
    dist = (dist_dir or PROJECT_ROOT / "frontend" / "dist").resolve()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await okx.start()
        try:
            yield
        finally:
            await okx.close()

    application = FastAPI(title="ReBS OKX Market Terminal", version="0.4.1", lifespan=lifespan)
    application.state.market = market
    application.state.okx = okx
    application.state.demo = demo
    application.include_router(create_strategy_router(demo.access_token))

    @application.exception_handler(DemoError)
    async def demo_error_handler(request: Request, exc: DemoError):
        return JSONResponse(status_code=502, content={'detail':str(exc)},headers={'Cache-Control':'no-store'})

    @application.get('/api/okx/demo/status')
    def demo_status():
        return JSONResponse(demo.status(),headers={'Cache-Control':'no-store'})

    @application.get('/api/okx/demo/overview')
    async def demo_overview(request: Request, fresh: bool = False):
        require_private_access(request,demo.access_token)
        return JSONResponse(await demo.overview(fresh),headers={'Cache-Control':'no-store'})

    @application.post('/api/okx/demo/order')
    async def demo_order(request: Request, order: DemoOrder):
        require_private_access(request,demo.access_token)
        return JSONResponse(await demo.place(order.model_dump()),headers={'Cache-Control':'no-store'})

    @application.get('/api/okx/demo/order')
    async def demo_order_status(request: Request, instId: str, clOrdId: str = Query(pattern=r'^[a-f0-9]{32}$')):
        require_private_access(request,demo.access_token)
        return JSONResponse(await demo.resolve(instId,clOrdId),headers={'Cache-Control':'no-store'})

    @application.post('/api/okx/demo/cancel')
    async def demo_cancel(request: Request, order: DemoCancel):
        require_private_access(request,demo.access_token)
        return JSONResponse(await demo.cancel(order.instId,order.ordId),headers={'Cache-Control':'no-store'})

    @application.exception_handler(InputError)
    async def input_error_handler(request: Request, exc: InputError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @application.exception_handler(DataError)
    async def data_error_handler(request: Request, exc: DataError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @application.exception_handler(UpstreamError)
    async def upstream_error_handler(request: Request, exc: UpstreamError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @application.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.4.1", "mode": "crypto-spot", "source": "OKX", "trading": False, "demoTrading": True, "strategyWorkbench": True, "strategyExecution": False}

    @application.get("/api/okx/markets")
    async def okx_markets():
        return await okx.markets()

    @application.get("/api/okx/candles")
    async def okx_candles(instId: str = "BTC-USDT", period: str = "15m",
                          before: int | None = Query(default=None, gt=0),
                          limit: int = Query(default=300, ge=1, le=300)):
        return await okx.candles(instId, period, before, limit)

    @application.get("/api/okx/snapshot")
    async def okx_snapshot(instId: str = "BTC-USDT"):
        return await okx.snapshot(instId)

    @application.websocket("/api/okx/stream")
    async def okx_stream(socket: WebSocket, instId: str = "BTC-USDT", period: str = "15m"):
        await socket.accept()
        try:
            instId, period = instrument_id(instId), period_id(period)
        except InputError as exc:
            await socket.send_json({'type': 'error', 'message': str(exc)})
            await socket.close(code=1008)
            return
        await okx.stream(socket, instId, period)

    @application.get("/api/stocks")
    def stocks(q: str = Query(default="", max_length=60)):
        return market.search(q)

    @application.get("/api/history")
    def history(
        symbol: str = "sz002027",
        adjust: Literal["qfq", "hfq", "none"] = "qfq",
        before: int | None = Query(default=None, gt=0),
        limit: int = Query(default=400, ge=1, le=2000),
        refresh: bool = False,
    ):
        return market.history(symbol=symbol, adjust=adjust, before=before, limit=limit, refresh=refresh)

    # Register after /api routes, so an unknown API never receives SPA HTML.
    @application.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found")
        candidate = (dist / path).resolve()
        if not candidate.is_relative_to(dist):
            raise HTTPException(status_code=404, detail="Not found")
        if candidate.is_file():
            return FileResponse(candidate)
        index = dist / "index.html"
        if index.is_file() and not Path(path).suffix:
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="前端尚未构建，请启动 Vite 或先执行 npm run build")

    return application


app = create_app()
