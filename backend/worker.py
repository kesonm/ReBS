"""Isolated network worker: a parent process enforces a total wall-clock deadline.

AKShare's Tencent start-date helper has no request timeout. Isolation bounds that
helper too, without patching requests globally in the API server.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import json
import sys


def validate_raw(symbol: str, samples: list[dict]) -> bool:
    """Verify legacy `bfq` CSV prices against Tencent's documented empty adjust.

    `bfq` is not an AKShare input value, so a file name alone is not sufficient
    provenance for importing it as unadjusted prices.
    """
    import requests
    from akshare.utils import demjson

    for sample in samples:
        year = sample["date"][:4]
        response = requests.get(
            "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
            params={
                "_var": f"kline_day{year}",
                "param": f"{symbol},day,{year}-01-01,{year}-12-31,640,",
            },
            timeout=8,
        )
        response.raise_for_status()
        text = response.text
        payload = demjson.decode(text[text.find("={") + 1 :])
        rows = payload.get("data", {}).get(symbol, {}).get("day", [])
        match = next((row for row in rows if row[0] == sample["date"]), None)
        if match is None:
            return False
        for field, index in (("open", 1), ("close", 2), ("high", 3), ("low", 4)):
            if abs(float(match[index]) - float(sample[field])) > 0.0001:
                return False
    return bool(samples)


def execute(payload: dict):
    import akshare as ak

    if payload["task"] == "directory":
        return ak.stock_info_a_code_name().to_dict(orient="records")
    if payload["task"] == "validate_raw":
        return validate_raw(payload["symbol"], payload["samples"])
    if payload["task"] == "history":
        # Always use the documented empty string for unadjusted prices.
        adjust = "" if payload["adjust"] == "none" else payload["adjust"]
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
        frame = ak.stock_zh_a_hist_tx(
            symbol=payload["symbol"],
            start_date="19000101",
            end_date=today,
            adjust=adjust,
            timeout=8,
        )
        # AKShare 1.19.1 exempts every sz000 symbol from its lots-to-shares
        # conversion, inadvertently including Shenzhen main-board stocks.
        # Our API accepts A shares only (never indices), so correct that case.
        # Revisit this workaround when upgrading the pinned AKShare version.
        if payload["symbol"].startswith("sz000") and "volume" in frame:
            frame["volume"] = frame["volume"] * 100
        # JSON null, rather than NaN, and ISO dates are part of the wire contract.
        return json.loads(frame.to_json(orient="records", date_format="iso"))
    raise ValueError("Unknown worker task")


if __name__ == "__main__":
    try:
        request = json.loads(sys.argv[1])
        # AKShare may print progress; reserve stdout exclusively for JSON.
        with contextlib.redirect_stdout(sys.stderr):
            result = execute(request)
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=True, allow_nan=False))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}, ensure_ascii=True))
        sys.exit(1)
