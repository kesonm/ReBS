"""Data validation, bounded upstream calls and adjustment-isolated disk caches."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from threading import Lock
import time
from typing import Callable
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = timezone(timedelta(hours=8))
SOURCE = "腾讯证券 / AKShare"
ADJUSTMENTS = {"qfq", "hfq", "none"}
SEED_STOCKS = {
    "sz002027": "分众传媒",
    "sh600519": "贵州茅台",
    "sz000001": "平安银行",
    "sz000858": "五粮液",
    "sz300750": "宁德时代",
    "sz002594": "比亚迪",
    "sh600036": "招商银行",
    "sh601318": "中国平安",
    "sh601012": "隆基绿能",
    "sh688981": "中芯国际",
    "sz000333": "美的集团",
    "sh600900": "长江电力",
}


class InputError(ValueError):
    pass


class DataError(ValueError):
    pass


class UpstreamError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def normalize_symbol(value: str) -> str:
    value = value.strip().lower()
    match = re.fullmatch(r"(?:(sh|sz|bj))?(\d{6})", value)
    if not match:
        raise InputError("请输入 6 位沪深 A 股代码，例如 002027 或 sh600519")
    given_exchange, code = match.groups()
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        exchange = "sh"
    elif code.startswith(("000", "001", "002", "003", "300", "301")):
        exchange = "sz"
    else:
        raise InputError("第一版支持沪深 A 股，暂不支持北交所、指数、基金或港美股")
    if given_exchange and given_exchange != exchange:
        raise InputError("股票代码与交易所前缀不匹配")
    return exchange + code


def stock_record(symbol: str, name: str | None = None) -> dict:
    return {
        "symbol": symbol,
        "code": symbol[2:],
        "name": name or SEED_STOCKS.get(symbol, symbol[2:]),
        "exchange": symbol[:2].upper(),
    }


def finite_number(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DataError(f"行情字段 {label} 缺失或格式错误") from exc
    if not math.isfinite(result):
        raise DataError(f"行情字段 {label} 不是有效数字")
    return result


def normalize_rows(rows: list[dict]) -> list[dict]:
    """AKShare 1.19.1: volume=shares, amount=CNY, turnover=fraction.

    In particular `turnover` is a rate, not the chart library's traded amount.
    Adjusted prices can be negative and must not be discarded.
    """
    by_timestamp = {}
    for row in rows:
        try:
            day = datetime.strptime(str(row["date"])[:10], "%Y-%m-%d").replace(tzinfo=SHANGHAI)
        except (KeyError, ValueError, TypeError) as exc:
            raise DataError("行情日期缺失或格式错误") from exc
        bar = {"timestamp": int(day.timestamp() * 1000)}
        for key in ("open", "high", "low", "close", "volume", "amount"):
            bar[key] = finite_number(row.get(key), key)
        if bar["volume"] < 0 or bar["amount"] < 0:
            raise DataError("成交量或成交额不能为负数")
        if bar["high"] + 0.0001 < max(bar["open"], bar["close"], bar["low"]):
            raise DataError("行情最高价与开收盘价不一致")
        if bar["low"] - 0.0001 > min(bar["open"], bar["close"], bar["high"]):
            raise DataError("行情最低价与开收盘价不一致")
        rate = row.get("turnover")
        bar["turnoverRate"] = None if rate in (None, "") else finite_number(rate, "turnover")
        by_timestamp[bar["timestamp"]] = bar
    if not by_timestamp:
        raise DataError("数据源没有返回日线，请确认股票代码是否存在及是否上市")
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def run_worker(payload: dict, timeout: int = 60):
    try:
        process = subprocess.run(
            [sys.executable, "-m", "backend.worker", json.dumps(payload, ensure_ascii=True)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise UpstreamError(f"行情源超过 {timeout} 秒未完成响应，请稍后重试或检查网络代理") from exc
    except OSError as exc:
        raise UpstreamError("无法启动行情获取进程，请检查 Python 环境") from exc
    try:
        response = json.loads(process.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise UpstreamError("行情获取进程未返回有效数据，请检查 AKShare 安装") from exc
    if process.returncode or not response.get("ok"):
        error = response.get("error", "UnknownError")
        raise UpstreamError(f"腾讯或股票名录服务请求失败（{error}），请检查网络、代理或稍后重试")
    return response["data"]


class MarketService:
    def __init__(self, root: Path = PROJECT_ROOT, worker: Callable = run_worker):
        self.root = root
        self.cache_dir = root / ".cache" / "market"
        self.worker = worker
        self._locks: dict[str, Lock] = {}
        self._locks_guard = Lock()
        self._memory: dict[str, dict] = {}
        self._directory: list[dict] = []
        self._directory_complete = False
        self._directory_attempt = 0.0
        self._directory_lock = Lock()
        self._read_directory_cache()

    def _lock(self, key: str) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, Lock())

    def _save_json(self, path: Path, content: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(content, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_directory_cache(self):
        path = self.cache_dir / "stocks.json"
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            stocks = content["items"]
            self._directory = [stock_record(normalize_symbol(item["symbol"]), str(item["name"])) for item in stocks]
            self._directory_complete = bool(self._directory)
            fetched = datetime.fromisoformat(content["fetchedAt"])
            if fetched.tzinfo and datetime.now(SHANGHAI) - fetched < timedelta(days=7):
                self._directory_attempt = time.monotonic()
        except (OSError, ValueError, KeyError, TypeError):
            self._directory = []

    def warm_directory(self):
        # At most one request every 10 minutes, including a failed request.
        with self._directory_lock:
            if self._directory_attempt and time.monotonic() - self._directory_attempt < 600:
                return
            self._directory_attempt = time.monotonic()
            try:
                rows = self.worker({"task": "directory"}, timeout=30)
                stocks = {}
                for item in rows:
                    try:
                        symbol = normalize_symbol(str(item["code"]).zfill(6))
                        stocks[symbol] = stock_record(symbol, str(item["name"]))
                    except (InputError, KeyError):
                        continue
                if not stocks:
                    raise DataError("股票名录为空")
                self._directory = list(stocks.values())
                self._directory_complete = True
                self._save_json(self.cache_dir / "stocks.json", {"fetchedAt": now_iso(), "items": self._directory})
            except (UpstreamError, DataError, OSError):
                # A usable old directory is preferable to losing name search.
                pass

    def get_stock(self, symbol: str) -> dict:
        return next((item for item in self._directory if item["symbol"] == symbol), stock_record(symbol))

    def search(self, query: str) -> dict:
        query = query.strip().lower()
        limited = not self._directory_complete
        stocks = self._directory or [stock_record(symbol, name) for symbol, name in SEED_STOCKS.items()]
        if re.fullmatch(r"(?:(sh|sz|bj))?\d{6}", query):
            symbol = normalize_symbol(query)
            # Code lookup works even when the exchange directory is unavailable.
            return {"items": [self.get_stock(symbol)], "limited": limited}
        matches = [item for item in stocks if not query or query in item["code"] or query in item["name"].lower() or query in item["symbol"]]
        if query and not matches and not self._directory_complete:
            self.warm_directory()
            stocks = self._directory or stocks
            matches = [item for item in stocks if query in item["code"] or query in item["name"].lower() or query in item["symbol"]]
        return {"items": matches[:30], "limited": not self._directory_complete}

    def _read_history_cache(self, key: str) -> dict | None:
        if key in self._memory:
            return self._memory[key]
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            if content.get("schema") != 1 or f"{content['symbol']}_{content['adjust']}" != key:
                raise DataError("行情缓存口径不匹配")
            bars = content["bars"]
            if not bars or any(bars[i]["timestamp"] >= bars[i + 1]["timestamp"] for i in range(len(bars) - 1)):
                raise DataError("行情缓存日期顺序无效")
            for bar in bars:
                for field in ("timestamp", "open", "high", "low", "close", "volume", "amount"):
                    finite_number(bar[field], field)
            if datetime.fromisoformat(content["fetchedAt"]).tzinfo is None:
                raise DataError("行情缓存时间缺少时区")
            self._memory[key] = content
            return content
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DataError("本地行情缓存损坏，请点击刷新重新获取") from exc

    def _legacy_csv(self, symbol: str, adjust: str) -> dict | None:
        suffix = "bfq" if adjust == "none" else adjust
        path = self.root / f"{symbol}_daily_{suffix}.csv"
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            bars = normalize_rows(rows)
        except (OSError, csv.Error, UnicodeError, DataError) as exc:
            raise DataError(f"本地文件 {path.name} 无法读取或行情字段无效，请点击刷新重新获取") from exc
        source = f"{SOURCE} · 本地 CSV"
        if adjust == "none":
            samples = [rows[index] for index in sorted({0, len(rows) // 2, len(rows) - 1})]
            try:
                verified = self.worker({"task": "validate_raw", "symbol": symbol, "samples": samples}, timeout=30)
            except UpstreamError:
                verified = False
            if not verified:
                # Never silently label an unverified legacy file as raw prices.
                return None
            source += "（不复权价格已抽样核验）"
        content = {
            "schema": 1, "symbol": symbol, "adjust": adjust,
            "fetchedAt": datetime.fromtimestamp(path.stat().st_mtime, SHANGHAI).isoformat(timespec="seconds"),
            "source": source, "bars": bars,
        }
        self._save_json(self.cache_dir / f"{symbol}_{adjust}.json", content)
        return content

    def history(self, symbol: str, adjust: str = "qfq", before: int | None = None, limit: int = 400, refresh: bool = False) -> dict:
        symbol = normalize_symbol(symbol)
        if adjust not in ADJUSTMENTS:
            raise InputError("复权方式必须为 qfq、hfq 或 none")
        if not 1 <= limit <= 2000:
            raise InputError("每页数量必须在 1 到 2000 之间")
        key = f"{symbol}_{adjust}"
        with self._lock(key):
            cache_warning = None
            try:
                content = self._read_history_cache(key)
                if content is None:
                    content = self._legacy_csv(symbol, adjust)
            except DataError as exc:
                if not refresh:
                    raise
                content = None
                cache_warning = str(exc)
            cached = content is not None
            warning = cache_warning
            failed_refresh = False
            if content is None or refresh:
                try:
                    rows = self.worker({"task": "history", "symbol": symbol, "adjust": adjust}, timeout=60)
                    bars = normalize_rows(rows)
                    content = {
                        "schema": 1, "symbol": symbol, "adjust": adjust,
                        "fetchedAt": now_iso(), "source": SOURCE, "bars": bars,
                    }
                    self._save_json(self.cache_dir / f"{key}.json", content)
                    cached = False
                    warning = None
                except (UpstreamError, DataError) as exc:
                    if content is None:
                        raise UpstreamError(str(exc)) from exc
                    failed_refresh = True
                    warning = f"刷新失败，继续显示缓存：{exc}"
            self._memory[key] = content
            bars = content["bars"]
            eligible = bars if before is None else [bar for bar in bars if bar["timestamp"] < before]
            page = eligible[-limit:]
            fetched = datetime.fromisoformat(content["fetchedAt"])
            stale = failed_refresh or datetime.now(SHANGHAI) - fetched > timedelta(hours=24)
            if stale and not warning:
                warning = "缓存超过 24 小时，点击刷新获取最新日线；当前展示的不是实时行情"
            response = {
                "stock": self.get_stock(symbol), "adjust": adjust, "bars": page,
                "hasMore": len(eligible) > limit, "source": content["source"],
                "fetchedAt": content["fetchedAt"], "cached": cached, "stale": stale,
                "total": len(bars),
            }
            if warning:
                response["warning"] = warning
            return response
