#!/usr/bin/env python3
"""运行 A 股行情快照接口测试，并生成不带排名的结果表。"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from market_benchmark.calendar import latest_completed_trade_date
from market_benchmark.models import parse_securities
from market_benchmark.providers import PROVIDER_FACTORIES
from market_benchmark.reporting import write_reports
from market_benchmark.runner import BenchmarkConfig, run_benchmark
from market_benchmark.scenarios import SNAPSHOT_SCENARIOS

DEFAULT_PROVIDERS = "fqgate,akshare,easyquotation,mootdx,tushare"
DEFAULT_SCENARIOS = ",".join(SNAPSHOT_SCENARIOS)
DEFAULT_SYMBOLS = "600519,601318,600036"


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能是负数")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("不能是负数")
    return parsed


def parse_selection(value: str, choices: set[str], label: str) -> tuple[str, ...]:
    names = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    unknown = sorted(set(names) - choices)
    if not names:
        raise argparse.ArgumentTypeError(f"至少选择一个{label}")
    if unknown:
        raise argparse.ArgumentTypeError(f"未知{label}：{', '.join(unknown)}")
    if len(set(names)) != len(names):
        raise argparse.ArgumentTypeError(f"{label}不能重复")
    return names


def parse_providers(value: str) -> tuple[str, ...]:
    return parse_selection(value, set(PROVIDER_FACTORIES), "比较项目")


def parse_scenarios(value: str) -> tuple[str, ...]:
    return parse_selection(value, set(SNAPSHOT_SCENARIOS), "快照场景")


def default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("benchmark-results") / f"market-snapshots-{stamp}.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--providers",
        type=parse_providers,
        default=parse_providers(DEFAULT_PROVIDERS),
        help=f"逗号分隔的项目，默认：{DEFAULT_PROVIDERS}",
    )
    parser.add_argument(
        "--scenarios",
        type=parse_scenarios,
        default=parse_scenarios(DEFAULT_SCENARIOS),
        help="逗号分隔的快照场景，默认运行全部",
    )
    parser.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help=f"逗号分隔的 6 位 A 股代码，默认：{DEFAULT_SYMBOLS}",
    )
    parser.add_argument("--rounds", type=positive_int, default=10, help="正式轮数")
    parser.add_argument(
        "--warmups", type=nonnegative_int, default=2, help="不计入统计的预热轮数"
    )
    parser.add_argument(
        "--history-count",
        type=positive_int,
        default=240,
        help="分钟 K、mootdx 日 K 和成交明细的目标条数",
    )
    parser.add_argument(
        "--trade-date",
        default=None,
        help="历史明细使用的完整交易日，格式 YYYYMMDD；默认由 FQGate 交易日历选择",
    )
    parser.add_argument(
        "--interval",
        type=nonnegative_float,
        default=0.5,
        help="项目调用间隔秒数，不计入耗时",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="网络超时秒数")
    parser.add_argument("--seed", type=int, default=20260913, help="调用顺序随机种子")
    parser.add_argument(
        "--fqgate-url", default="http://127.0.0.1:17281", help="FQGate 本机地址"
    )
    parser.add_argument("--output", type=Path, default=None, help="JSON 报告路径")
    parser.add_argument("--list-providers", action="store_true", help="列出项目")
    parser.add_argument("--list-scenarios", action="store_true", help="列出快照场景")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_providers:
        print("\n".join(PROVIDER_FACTORIES))
        return 0
    if args.list_scenarios:
        for scenario in SNAPSHOT_SCENARIOS.values():
            print(f"{scenario.name}\t{scenario.display_name}")
        return 0
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")

    try:
        securities = parse_securities(args.symbols)
        trade_date = args.trade_date or latest_completed_trade_date(
            args.fqgate_url, args.timeout
        )
        parsed_trade_date = date(
            int(trade_date[:4]), int(trade_date[4:6]), int(trade_date[6:])
        )
        daily_start_date = (parsed_trade_date - timedelta(days=370)).strftime("%Y%m%d")
        config = BenchmarkConfig(
            securities=securities,
            providers=args.providers,
            scenarios=args.scenarios,
            trade_date=trade_date,
            daily_start_date=daily_start_date,
            rounds=args.rounds,
            warmups=args.warmups,
            history_count=args.history_count,
            interval_seconds=args.interval,
            timeout_seconds=args.timeout,
            seed=args.seed,
            base_url=args.fqgate_url,
        )
        report = run_benchmark(config)
        json_path, markdown_path = write_reports(
            report, args.output or default_output()
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"行情快照测试失败：{error}", file=sys.stderr)
        return 2

    print(f"JSON 测试记录：{json_path.resolve()}")
    print(f"Markdown 报告：{markdown_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
