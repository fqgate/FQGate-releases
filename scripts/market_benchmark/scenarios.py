"""用户常见的行情快照测试场景。"""

from __future__ import annotations

from .models import SnapshotScenario

SNAPSHOT_SCENARIOS: dict[str, SnapshotScenario] = {
    "selected_quote": SnapshotScenario(
        "selected_quote",
        "指定证券行情快照",
        "同一批 3 只 A 股",
        1,
    ),
    "market_snapshot": SnapshotScenario(
        "market_snapshot",
        "A 股全市场快照",
        "沪深京全市场",
        1000,
    ),
    "daily_kline": SnapshotScenario(
        "daily_kline",
        "历史日 K",
        "首只证券近一年",
        100,
    ),
    "minute_kline": SnapshotScenario(
        "minute_kline",
        "历史 1 分钟 K / 分时",
        "首只证券最近完整交易日",
        200,
    ),
    "trade_details": SnapshotScenario(
        "trade_details",
        "普通成交明细",
        "首只证券最近完整交易日",
        1,
    ),
    "depth_l1": SnapshotScenario(
        "depth_l1",
        "五档盘口快照",
        "同一批 3 只 A 股",
        1,
    ),
    "level2_transactions": SnapshotScenario(
        "level2_transactions",
        "Level-2 逐笔成交",
        "首只证券最近 200 条",
        1,
    ),
    "level2_orders": SnapshotScenario(
        "level2_orders",
        "Level-2 逐笔委托",
        "首只证券最近 200 条",
        1,
    ),
    "level2_buy_cancellations": SnapshotScenario(
        "level2_buy_cancellations",
        "Level-2 买入撤单",
        "首只证券最近 200 条",
        1,
    ),
    "level2_sell_cancellations": SnapshotScenario(
        "level2_sell_cancellations",
        "Level-2 卖出撤单",
        "首只证券最近 200 条",
        1,
    ),
}


DEFAULT_SCENARIOS = tuple(SNAPSHOT_SCENARIOS)
