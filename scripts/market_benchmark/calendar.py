"""选择最近一个已经结束的 A 股交易日。"""

from __future__ import annotations

import http.client
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

CHINA_TIMEZONE = timezone(timedelta(hours=8))


def latest_completed_trade_date(base_url: str, timeout_seconds: float) -> str:
    """通过 FQGate 交易日历取得今天以前最近的交易日。"""

    parsed = urlparse(base_url.rstrip("/"))
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("FQGate 基准地址必须是本机 HTTP 回环地址")
    today = datetime.now(CHINA_TIMEZONE).date()
    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=45)
    body = json.dumps(
        {
            "start_date": start_date.strftime("%Y%m%d"),
            "end_date": end_date.strftime("%Y%m%d"),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    connection = http.client.HTTPConnection(
        parsed.hostname, parsed.port or 80, timeout=timeout_seconds
    )
    try:
        connection.request(
            "POST",
            "/v1/market/calendar/trading-days",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        content = response.read()
    finally:
        connection.close()
    if response.status != 200:
        raise RuntimeError(f"FQGate 交易日历返回 HTTP {response.status}")
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("FQGate 交易日历没有返回有效 JSON") from error
    data = document.get("data") if isinstance(document, dict) else None
    records = data.get("records") if isinstance(data, dict) else None
    dates = sorted(
        value
        for value in (records or [])
        if isinstance(value, str) and len(value) == 8 and value.isdigit()
    )
    if not dates:
        raise RuntimeError("FQGate 交易日历没有返回最近的完整交易日")
    return dates[-1]
