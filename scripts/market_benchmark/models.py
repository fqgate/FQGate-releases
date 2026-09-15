"""行情快照基准测试的公共数据模型与输入解析。"""

from __future__ import annotations

from dataclasses import dataclass

MARKET_PREFIXES = {
    "SH": "USHA",
    "SZ": "USZA",
    "BJ": "USBJ",
    "USHA": "USHA",
    "USZA": "USZA",
    "USBJ": "USBJ",
}

DISPLAY_PREFIXES = {"USHA": "sh", "USZA": "sz", "USBJ": "bj"}


@dataclass(frozen=True, order=True)
class Security:
    """一个可在各行情库之间转换的沪深京证券标识。"""

    market: str
    code: str

    @property
    def fqgate_code(self) -> str:
        return f"{self.market}{self.code}"

    @property
    def prefixed_code(self) -> str:
        return f"{DISPLAY_PREFIXES[self.market]}{self.code}"


@dataclass(frozen=True)
class SnapshotScenario:
    """一个独立的快照测试场景。"""

    name: str
    display_name: str
    sample_description: str
    minimum_rows: int


@dataclass(frozen=True)
class SnapshotContext:
    """适配器执行快照查询时共享的固定参数。"""

    securities: tuple[Security, ...]
    trade_date: str
    daily_start_date: str
    history_count: int

    @property
    def primary_security(self) -> Security:
        return self.securities[0]


@dataclass(frozen=True)
class FetchResult:
    """一次查询的脱敏摘要，不保留价格、数量等行情原值。"""

    row_count: int
    provider_calls: int
    expected_items: int | None = None
    returned_items: int | None = None


def infer_market(code: str) -> str:
    if code.startswith("6"):
        return "USHA"
    if code.startswith(("0", "3")):
        return "USZA"
    if code.startswith(("43", "83", "87", "92")):
        return "USBJ"
    raise ValueError(f"代码不属于当前支持的沪深京 A 股范围：{code}")


def parse_security(value: str) -> Security:
    raw = value.strip().upper().replace(".", ":")
    if not raw:
        raise ValueError("证券代码不能为空")

    market = None
    code = raw
    if ":" in raw:
        prefix, code = raw.split(":", 1)
        market = MARKET_PREFIXES.get(prefix)
        if market is None:
            raise ValueError(f"不支持的市场前缀：{prefix}")
    else:
        for prefix in sorted(MARKET_PREFIXES, key=len, reverse=True):
            if raw.startswith(prefix):
                market = MARKET_PREFIXES[prefix]
                code = raw[len(prefix) :]
                break

    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"证券代码必须是 6 位数字：{value}")
    inferred_market = infer_market(code)
    if market is not None and market != inferred_market:
        raise ValueError(f"市场前缀与 A 股代码不匹配：{value}")
    return Security(market or inferred_market, code)


def parse_securities(value: str) -> tuple[Security, ...]:
    securities = tuple(
        parse_security(item) for item in value.split(",") if item.strip()
    )
    if not securities:
        raise ValueError("至少提供一个证券代码")
    if len(securities) > 100:
        raise ValueError("一次最多比较 100 只证券，避免对公共数据源造成过大压力")
    if len(set(securities)) != len(securities):
        raise ValueError("证券列表中不能包含重复代码")
    return securities
