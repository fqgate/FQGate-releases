"""FQGate 与主流开源行情项目的快照场景适配器。"""

from __future__ import annotations

import http.client
import importlib
import importlib.metadata
import json
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from urllib.parse import urlparse

from .models import FetchResult, Security, SnapshotContext


class ProviderPermissionRequired(RuntimeError):
    """项目存在对应接口，但当前账号或环境没有所需权限。"""


@dataclass(frozen=True)
class ProviderMetadata:
    name: str
    display_name: str
    project_url: str
    supported_scenarios: tuple[str, ...]
    version: str | None = None


def serialize_metadata(metadata: ProviderMetadata) -> dict:
    """报告字段统一使用 lowerCamelCase。"""
    return {
        "name": metadata.name,
        "displayName": metadata.display_name,
        "projectUrl": metadata.project_url,
        "supportedScenarios": list(metadata.supported_scenarios),
        "version": metadata.version,
    }


class MarketProvider(ABC):
    """适配器只返回数量和完整性摘要，不向报告传递行情原值。"""

    metadata: ProviderMetadata

    @abstractmethod
    def setup(self) -> None:
        pass

    @abstractmethod
    def fetch(self, scenario: str, context: SnapshotContext) -> FetchResult:
        pass

    def close(self) -> None:
        pass

    def supports(self, scenario: str) -> bool:
        return scenario in self.metadata.supported_scenarios

    def ensure_supported_markets(self, securities: tuple[Security, ...]) -> None:
        unsupported = sorted({item.market for item in securities} - self.markets)
        if unsupported:
            raise ValueError(f"该项目适配器尚未验证市场：{', '.join(unsupported)}")

    @property
    def markets(self) -> set[str]:
        return {"USHA", "USZA"}


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def normalize_code(value: object) -> str | None:
    text = str(value).strip().lower()
    for prefix in ("sh", "sz", "bj"):
        if text.startswith(prefix):
            text = text[2:]
            break
    return text if len(text) == 6 and text.isdigit() else None


def matched_security_count(
    codes: Iterable[object], requested: tuple[Security, ...]
) -> int:
    requested_codes = {item.code for item in requested}
    returned = {
        code for value in codes if (code := normalize_code(value)) in requested_codes
    }
    return len(returned)


def frame_row_count(frame: object) -> int:
    if frame is None or getattr(frame, "empty", True):
        return 0
    return len(frame)  # type: ignore[arg-type]


class FqgateProvider(MarketProvider):
    SUPPORTED = (
        "selected_quote",
        "daily_kline",
        "minute_kline",
        "trade_details",
        "depth_l1",
        "level2_transactions",
        "level2_orders",
        "level2_buy_cancellations",
        "level2_sell_cancellations",
    )

    def __init__(self, base_url: str, timeout_seconds: float):
        parsed = urlparse(base_url.rstrip("/"))
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("FQGate 基准地址必须是本机 HTTP 回环地址")
        if parsed.username or parsed.password:
            raise ValueError("FQGate 基准地址不能包含用户名或密码")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("FQGate 基准地址不能包含路径、查询参数或片段")
        self._host = parsed.hostname
        self._port = parsed.port or 80
        self._timeout = timeout_seconds
        self._connection: http.client.HTTPConnection | None = None
        self.metadata = ProviderMetadata(
            "fqgate",
            "FQGate",
            "https://github.com/zhuyifang/fqgate-releases",
            self.SUPPORTED,
        )

    @property
    def markets(self) -> set[str]:
        return {"USHA", "USZA", "USBJ"}

    def _connect(self) -> http.client.HTTPConnection:
        if self._connection is None:
            self._connection = http.client.HTTPConnection(
                self._host, self._port, timeout=self._timeout
            )
        return self._connection

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        payload = (
            None
            if body is None
            else json.dumps(body, separators=(",", ":")).encode("utf-8")
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Request-Timeout-Ms": str(max(1, round(self._timeout * 1000))),
        }
        try:
            connection = self._connect()
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            content = response.read()
        except Exception:
            self.close()
            raise

        try:
            document = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("FQGate 返回内容不是有效 JSON") from error
        if not isinstance(document, dict):
            raise RuntimeError("FQGate 返回结构不是 JSON 对象")  # noqa: TRY004
        if response.status == 409:
            raise ProviderPermissionRequired(
                str(document.get("message") or "当前行情账号没有对应权限")
            )
        if response.status != 200:
            raise RuntimeError(f"FQGate 返回 HTTP {response.status}")
        if "code" in document and document.get("code") != 0:
            raise RuntimeError(f"FQGate 行情业务返回错误码 {document.get('code')}")
        return document

    @staticmethod
    def _records(document: dict) -> list[dict]:
        data = document.get("data") if isinstance(document.get("data"), dict) else {}
        rows: list[dict] = []
        for item in data.get("records") or []:
            if isinstance(item, list):
                rows.extend(row for row in item if isinstance(row, dict))
            elif isinstance(item, dict):
                rows.append(item)
        return rows

    @classmethod
    def _row_count(cls, document: dict) -> int:
        data = document.get("data") if isinstance(document.get("data"), dict) else {}
        declared = data.get("row_count")
        return declared if isinstance(declared, int) else len(cls._records(document))

    @staticmethod
    def _securities(context: SnapshotContext) -> list[dict[str, str]]:
        return [
            {"market": item.market, "code": item.code} for item in context.securities
        ]

    def setup(self) -> None:
        specification = self._request("GET", "/openapi.json")
        info = (
            specification.get("info")
            if isinstance(specification.get("info"), dict)
            else {}
        )
        health = self._request("GET", "/v1/market/health")
        data = health.get("data") if isinstance(health.get("data"), dict) else {}
        if data.get("status") != "ok":
            raise RuntimeError("FQGate 行情服务健康检查未通过")
        self.metadata = replace(
            self.metadata, version=str(info.get("version") or "unknown")
        )

    def _selected_quote(self, context: SnapshotContext) -> FetchResult:
        grouped: dict[str, list[Security]] = defaultdict(list)
        for security in context.securities:
            grouped[security.market].append(security)
        matched = 0
        rows = 0
        for group in grouped.values():
            response = self._request(
                "POST",
                "/v1/market/realtime/quote",
                {
                    "securities": [
                        {"market": item.market, "code": item.code} for item in group
                    ],
                    "fields": [5, 6, 7, 8, 9, 10, 12, 13, 18, 19, 48, 49, 55],
                },
            )
            records = self._records(response)
            identities = []
            for row in records:
                identity = row.get("5")
                if isinstance(identity, dict):
                    identity = identity.get("value")
                identities.append(identity)
            expected = {item.fqgate_code for item in group}
            matched += len(expected & {str(value) for value in identities})
            rows += self._row_count(response)
        return FetchResult(rows, len(grouped), len(context.securities), matched)

    def _kline(self, context: SnapshotContext, interval: str) -> FetchResult:
        security = context.primary_security
        body = {
            "market": security.market,
            "code": security.code,
            "interval": interval,
            "adjust": "",
        }
        if interval == "day":
            body.update(
                {
                    "start_date": context.daily_start_date,
                    "end_date": context.trade_date,
                }
            )
        else:
            body["count"] = context.history_count
        response = self._request("POST", "/v1/market/history/klines", body)
        return FetchResult(self._row_count(response), 1)

    def _level2(self, context: SnapshotContext, scenario: str) -> FetchResult:
        security = context.primary_security
        paths = {
            "level2_transactions": "/v1/market/level2/transactions",
            "level2_orders": "/v1/market/level2/orders",
            "level2_buy_cancellations": "/v1/market/level2/cancellations/buy",
            "level2_sell_cancellations": "/v1/market/level2/cancellations/sell",
        }
        if scenario == "level2_transactions":
            body = {
                "market": security.market,
                "code": security.code,
                "fields": [
                    "source_tick_id",
                    "price",
                    "side",
                    "volume",
                    "trade_time",
                ],
                "count": 200,
                "end_time": 0,
                "require_data": True,
                "semantic": True,
                "trade_date": context.trade_date,
            }
        else:
            body = {
                "market": security.market,
                "code": security.code,
                "fields": [1, 10, 12, 13, 18, 56, 74, 75],
                "range": {"mode": "recent", "count": 200, "end": 0},
                "require_data": True,
                "semantic": True,
                "trade_date": context.trade_date,
            }
        response = self._request("POST", paths[scenario], body)
        return FetchResult(self._row_count(response), 1)

    def fetch(self, scenario: str, context: SnapshotContext) -> FetchResult:
        self.ensure_supported_markets(context.securities)
        if scenario == "selected_quote":
            return self._selected_quote(context)
        if scenario == "daily_kline":
            return self._kline(context, "day")
        if scenario == "minute_kline":
            return self._kline(context, "1m")
        if scenario == "trade_details":
            security = context.primary_security
            response = self._request(
                "POST",
                "/v1/market/history/tick",
                {"market": security.market, "code": security.code},
            )
            return FetchResult(self._row_count(response), 1)
        if scenario == "depth_l1":
            response = self._request(
                "POST",
                "/v1/market/history/depth",
                {"securities": self._securities(context)},
            )
            rows = self._row_count(response)
            return FetchResult(rows, 1, len(context.securities), rows)
        if scenario.startswith("level2_"):
            return self._level2(context, scenario)
        raise ValueError(f"FQGate 未实现快照场景：{scenario}")

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None


class AkshareProvider(MarketProvider):
    SUPPORTED = (
        "selected_quote",
        "market_snapshot",
        "daily_kline",
        "minute_kline",
        "trade_details",
        "depth_l1",
    )

    def __init__(self, _base_url: str, timeout_seconds: float):
        self._module = None
        self._timeout = timeout_seconds
        self.metadata = ProviderMetadata(
            "akshare",
            "AKShare",
            "https://github.com/akfamily/akshare",
            self.SUPPORTED,
        )

    def setup(self) -> None:
        self._module = importlib.import_module("akshare")
        self.metadata = replace(self.metadata, version=package_version("akshare"))

    def _quote_rows(self, context: SnapshotContext) -> FetchResult:
        matched = 0
        rows = 0
        for security in context.securities:
            frame = self._module.stock_bid_ask_em(symbol=security.code)
            count = frame_row_count(frame)
            rows += count
            if count:
                matched += 1
        return FetchResult(
            rows, len(context.securities), len(context.securities), matched
        )

    def fetch(self, scenario: str, context: SnapshotContext) -> FetchResult:
        self.ensure_supported_markets(context.securities)
        security = context.primary_security
        if scenario in {"selected_quote", "depth_l1"}:
            return self._quote_rows(context)
        if scenario == "market_snapshot":
            frame = self._module.stock_zh_a_spot_em()
            return FetchResult(frame_row_count(frame), 1)
        if scenario == "daily_kline":
            frame = self._module.stock_zh_a_hist(
                symbol=security.code,
                period="daily",
                start_date=context.daily_start_date,
                end_date=context.trade_date,
                adjust="",
                timeout=self._timeout,
            )
            return FetchResult(frame_row_count(frame), 1)
        if scenario == "minute_kline":
            trade_date = context.trade_date
            frame = self._module.stock_zh_a_hist_min_em(
                symbol=security.code,
                start_date=f"{trade_date} 09:30:00",
                end_date=f"{trade_date} 15:00:00",
                period="1",
                adjust="",
            )
            return FetchResult(frame_row_count(frame), 1)
        if scenario == "trade_details":
            frame = self._module.stock_intraday_em(symbol=security.code)
            return FetchResult(frame_row_count(frame), 1)
        raise ValueError(f"AKShare 未实现快照场景：{scenario}")


class EasyQuotationProvider(MarketProvider):
    SUPPORTED = (
        "selected_quote",
        "market_snapshot",
        "minute_kline",
        "depth_l1",
    )

    def __init__(self, _base_url: str, _timeout_seconds: float):
        self._quote_client = None
        self._minute_client = None
        self.metadata = ProviderMetadata(
            "easyquotation",
            "easyquotation",
            "https://github.com/shidenggui/easyquotation",
            self.SUPPORTED,
        )

    @property
    def markets(self) -> set[str]:
        return {"USHA", "USZA", "USBJ"}

    def setup(self) -> None:
        module = importlib.import_module("easyquotation")
        self._quote_client = module.use("tencent")
        self._minute_client = module.use("timekline")
        self.metadata = replace(self.metadata, version=package_version("easyquotation"))

    def _quote(self, context: SnapshotContext) -> FetchResult:
        result = self._quote_client.real(
            [item.prefixed_code for item in context.securities], prefix=True
        )
        if not isinstance(result, dict):
            return FetchResult(0, 1, len(context.securities), 0)
        matched = matched_security_count(result.keys(), context.securities)
        return FetchResult(len(result), 1, len(context.securities), matched)

    def fetch(self, scenario: str, context: SnapshotContext) -> FetchResult:
        self.ensure_supported_markets(context.securities)
        if scenario in {"selected_quote", "depth_l1"}:
            return self._quote(context)
        if scenario == "market_snapshot":
            result = self._quote_client.market_snapshot(prefix=True)
            return FetchResult(len(result) if isinstance(result, dict) else 0, 1)
        if scenario == "minute_kline":
            security = context.primary_security
            result = self._minute_client.real([security.prefixed_code], prefix=True)
            if not isinstance(result, dict) or not result:
                return FetchResult(0, 1)
            value = next(iter(result.values()))
            rows = len(value.get("time_data") or []) if isinstance(value, dict) else 0
            return FetchResult(rows, 1)
        raise ValueError(f"easyquotation 未实现快照场景：{scenario}")


class MootdxProvider(MarketProvider):
    SUPPORTED = (
        "selected_quote",
        "daily_kline",
        "minute_kline",
        "trade_details",
        "depth_l1",
    )

    def __init__(self, _base_url: str, timeout_seconds: float):
        self._client = None
        self._timeout = timeout_seconds
        self.metadata = ProviderMetadata(
            "mootdx",
            "mootdx",
            "https://github.com/mootdx/mootdx",
            self.SUPPORTED,
        )

    def setup(self) -> None:
        logging.getLogger("mootdx").setLevel(logging.ERROR)
        module = importlib.import_module("mootdx.quotes")
        self._client = module.Quotes.factory(
            market="std",
            bestip=True,
            timeout=max(1, round(self._timeout)),
            quiet=True,
        )
        self.metadata = replace(self.metadata, version=package_version("mootdx"))

    def _quote(self, context: SnapshotContext) -> FetchResult:
        frame = self._client.quotes(symbol=[item.code for item in context.securities])
        rows = frame_row_count(frame)
        codes = frame["code"] if rows and "code" in frame.columns else []
        matched = matched_security_count(codes, context.securities)
        return FetchResult(rows, 1, len(context.securities), matched)

    def fetch(self, scenario: str, context: SnapshotContext) -> FetchResult:
        self.ensure_supported_markets(context.securities)
        security = context.primary_security
        if scenario in {"selected_quote", "depth_l1"}:
            return self._quote(context)
        if scenario == "daily_kline":
            frame = self._client.bars(
                symbol=security.code, frequency=9, start=0, offset=context.history_count
            )
            return FetchResult(frame_row_count(frame), 1)
        if scenario == "minute_kline":
            frame = self._client.bars(
                symbol=security.code, frequency=8, start=0, offset=context.history_count
            )
            return FetchResult(frame_row_count(frame), 1)
        if scenario == "trade_details":
            frame = self._client.transactions(
                symbol=security.code,
                start=0,
                offset=min(context.history_count, 800),
                date=context.trade_date,
            )
            return FetchResult(frame_row_count(frame), 1)
        raise ValueError(f"mootdx 未实现快照场景：{scenario}")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class TushareProvider(MarketProvider):
    SUPPORTED = (
        "selected_quote",
        "market_snapshot",
        "daily_kline",
        "minute_kline",
        "trade_details",
        "depth_l1",
    )

    def __init__(self, _base_url: str, _timeout_seconds: float):
        self._module = None
        self.metadata = ProviderMetadata(
            "tushare",
            "Tushare（旧版公开接口）",
            "https://github.com/waditu/tushare",
            self.SUPPORTED,
        )

    def setup(self) -> None:
        self._module = importlib.import_module("tushare")
        self.metadata = replace(self.metadata, version=package_version("tushare"))

    @staticmethod
    def _display_date(value: str) -> str:
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"

    def _quote(self, context: SnapshotContext) -> FetchResult:
        frame = self._module.get_realtime_quotes(
            [item.code for item in context.securities]
        )
        rows = frame_row_count(frame)
        codes = frame["code"] if rows and "code" in frame.columns else []
        matched = matched_security_count(codes, context.securities)
        return FetchResult(rows, 1, len(context.securities), matched)

    def fetch(self, scenario: str, context: SnapshotContext) -> FetchResult:
        self.ensure_supported_markets(context.securities)
        security = context.primary_security
        if scenario in {"selected_quote", "depth_l1"}:
            return self._quote(context)
        if scenario == "market_snapshot":
            return FetchResult(frame_row_count(self._module.get_today_all()), 1)
        if scenario == "daily_kline":
            frame = self._module.get_k_data(
                security.code,
                start=self._display_date(context.daily_start_date),
                end=self._display_date(context.trade_date),
                ktype="D",
                autype=None,
                retry_count=1,
            )
            return FetchResult(frame_row_count(frame), 1)
        if scenario == "minute_kline":
            trade_date = self._display_date(context.trade_date)
            frame = self._module.get_k_data(
                security.code,
                start=trade_date,
                end=trade_date,
                ktype="1",
                autype=None,
                retry_count=1,
            )
            return FetchResult(frame_row_count(frame), 1)
        if scenario == "trade_details":
            frame = self._module.get_tick_data(
                security.code,
                date=self._display_date(context.trade_date),
                retry_count=1,
            )
            return FetchResult(frame_row_count(frame), 1)
        raise ValueError(f"Tushare 未实现快照场景：{scenario}")


ProviderFactory = Callable[[str, float], MarketProvider]

PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "fqgate": FqgateProvider,
    "akshare": AkshareProvider,
    "easyquotation": EasyQuotationProvider,
    "mootdx": MootdxProvider,
    "tushare": TushareProvider,
}
