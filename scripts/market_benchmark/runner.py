"""快照基准调度、失败隔离与统计汇总。"""

from __future__ import annotations

import math
import os
import platform
import random
import re
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone

from .models import Security, SnapshotContext
from .networking import direct_network_environment
from .providers import (
    PROVIDER_FACTORIES,
    MarketProvider,
    ProviderFactory,
    ProviderPermissionRequired,
    serialize_metadata,
)
from .scenarios import SNAPSHOT_SCENARIOS


@dataclass(frozen=True)
class BenchmarkConfig:
    securities: tuple[Security, ...]
    providers: tuple[str, ...]
    scenarios: tuple[str, ...]
    trade_date: str
    daily_start_date: str
    rounds: int = 10
    warmups: int = 2
    history_count: int = 240
    interval_seconds: float = 0.5
    timeout_seconds: float = 15.0
    seed: int = 20260913
    base_url: str = "http://127.0.0.1:17281"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_error(error: Exception) -> dict[str, str]:
    message = str(error).replace("\r\n", "\n").replace("\r", "\n")
    # 保留诊断所需的 URL 和响应内容，仅隐藏真正的认证信息。
    message = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", message, flags=re.IGNORECASE)
    message = re.sub(
        r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\n]*",
        r"\1***",
        message,
    )
    message = re.sub(
        r"(?i)\b(authorization|proxy-authorization)(\s*[=:]\s*)"
        r"(?:bearer\s+|basic\s+)?[A-Za-z0-9._~+/=-]+",
        r"\1\2***",
        message,
    )
    message = re.sub(
        r"(?i)(token|access[_-]?token|api[_-]?key|password|passwd|authorization|cookie)"
        r"([=:]\s*)([^&\s,;}]+)",
        r"\1\2***",
        message,
    )
    message = re.sub(
        r'(?i)(["\'](?:token|access[_-]?token|api[_-]?key|password|passwd|authorization|cookie)["\']\s*:\s*)'
        r'(["\'])(.*?)(\2)',
        r"\1\2***\4",
        message,
    )
    return {
        "type": type(error).__name__,
        "message": message or "未提供错误说明",
    }


def nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def summarize_samples(samples: list[dict]) -> dict:
    successful = [sample for sample in samples if sample["status"] == "success"]
    durations = [sample["elapsedMs"] for sample in successful]
    statuses = {sample["status"] for sample in samples}
    if samples and len(successful) == len(samples):
        status = "completed"
    elif statuses == {"permissionRequired"}:
        status = "permissionRequired"
    elif not successful and statuses == {"error"}:
        status = "failed"
    else:
        status = "incomplete"

    latency = None
    if durations:
        latency = {
            "minimum": round(min(durations), 3),
            "p50": round(nearest_rank(durations, 0.50), 3),
            "p95": round(nearest_rank(durations, 0.95), 3),
            "maximum": round(max(durations), 3),
            "mean": round(statistics.fmean(durations), 3),
        }
    observed = [sample for sample in samples if "rowCount" in sample]
    rows = [sample["rowCount"] for sample in observed]
    calls = [sample["providerCalls"] for sample in observed]
    covered = [
        sample
        for sample in observed
        if sample.get("expectedItems") is not None
        and sample.get("returnedItems") is not None
    ]
    return {
        "status": status,
        "successCount": len(successful),
        "attemptCount": len(samples),
        "latencyMs": latency,
        "rowCount": {
            "minimum": min(rows),
            "maximum": max(rows),
            "mean": round(statistics.fmean(rows), 3),
        }
        if rows
        else None,
        "itemCoverage": {
            "expected": covered[0]["expectedItems"],
            "returnedMinimum": min(sample["returnedItems"] for sample in covered),
            "returnedMaximum": max(sample["returnedItems"] for sample in covered),
            "returnedMean": round(
                statistics.fmean(sample["returnedItems"] for sample in covered), 3
            ),
        }
        if covered
        else None,
        "providerCallsPerAttempt": round(statistics.fmean(calls), 3) if calls else None,
    }


def validate_config(
    config: BenchmarkConfig, available_providers: set[str] | None = None
) -> None:
    if config.rounds < 1:
        raise ValueError("正式轮数必须至少为 1")
    if config.warmups < 0:
        raise ValueError("预热轮数不能为负数")
    if config.history_count < 1:
        raise ValueError("历史记录数量必须至少为 1")
    if config.interval_seconds < 0:
        raise ValueError("请求间隔不能为负数")
    if config.timeout_seconds <= 0:
        raise ValueError("超时必须大于 0")
    if not config.providers:
        raise ValueError("至少选择一个比较项目")
    if not config.scenarios:
        raise ValueError("至少选择一个快照场景")
    unknown_providers = sorted(
        set(config.providers) - (available_providers or set(PROVIDER_FACTORIES))
    )
    if unknown_providers:
        raise ValueError(f"未知比较项目：{', '.join(unknown_providers)}")
    unknown_scenarios = sorted(set(config.scenarios) - set(SNAPSHOT_SCENARIOS))
    if unknown_scenarios:
        raise ValueError(f"未知快照场景：{', '.join(unknown_scenarios)}")
    for value, label in (
        (config.trade_date, "交易日"),
        (config.daily_start_date, "日 K 开始日期"),
    ):
        try:
            if len(value) != 8 or not value.isdigit():
                raise ValueError
            date(int(value[:4]), int(value[4:6]), int(value[6:]))
        except ValueError as error:
            raise ValueError(f"{label}必须使用 YYYYMMDD 格式：{value}") from error


def _empty_summary(status: str) -> dict:
    return {
        "status": status,
        "successCount": 0,
        "attemptCount": 0,
        "latencyMs": None,
        "rowCount": None,
        "itemCoverage": None,
        "providerCallsPerAttempt": None,
    }


def _run_benchmark(
    config: BenchmarkConfig,
    factories: dict[str, ProviderFactory] | None,
    sleep: Callable[[float], None],
) -> dict:
    provider_factories = factories or PROVIDER_FACTORIES
    validate_config(config, set(provider_factories))
    randomizer = random.Random(config.seed)
    started_at = utc_now()
    context = SnapshotContext(
        securities=config.securities,
        trade_date=config.trade_date,
        daily_start_date=config.daily_start_date,
        history_count=config.history_count,
    )
    providers: dict[str, dict] = {}
    active: dict[str, MarketProvider] = {}
    results: dict[str, dict[str, dict]] = {
        scenario: {} for scenario in config.scenarios
    }

    for name in config.providers:
        provider = provider_factories[name](config.base_url, config.timeout_seconds)
        setup_started = time.perf_counter_ns()
        try:
            provider.setup()
            provider.ensure_supported_markets(config.securities)
            active[name] = provider
            providers[name] = {
                "metadata": serialize_metadata(provider.metadata),
                "setupMs": round(
                    (time.perf_counter_ns() - setup_started) / 1_000_000, 3
                ),
            }
        except Exception as error:  # noqa: BLE001 - 单个项目初始化失败不能终止全表。
            providers[name] = {
                "metadata": serialize_metadata(provider.metadata),
                "setupMs": round(
                    (time.perf_counter_ns() - setup_started) / 1_000_000, 3
                ),
                "setupError": safe_error(error),
            }
            try:
                provider.close()
            except Exception:  # noqa: BLE001,S110
                pass

    for scenario_name in config.scenarios:
        scenario = SNAPSHOT_SCENARIOS[scenario_name]
        for name in config.providers:
            if name not in active:
                results[scenario_name][name] = {
                    "status": "unavailable",
                    "warmups": [],
                    "samples": [],
                    "summary": _empty_summary("unavailable"),
                }
            elif not active[name].supports(scenario_name):
                results[scenario_name][name] = {
                    "status": "unsupported",
                    "warmups": [],
                    "samples": [],
                    "summary": _empty_summary("unsupported"),
                }
            else:
                results[scenario_name][name] = {
                    "status": "pending",
                    "warmups": [],
                    "samples": [],
                }

        participants = [
            name
            for name in config.providers
            if name in active and active[name].supports(scenario_name)
        ]

        def execute(
            name: str,
            round_number: int,
            warmup: bool,
            current_scenario_name: str = scenario_name,
            current_scenario=scenario,
        ) -> None:
            call_started = time.perf_counter_ns()
            try:
                outcome = active[name].fetch(current_scenario_name, context)
                elapsed_ms = (time.perf_counter_ns() - call_started) / 1_000_000
                complete = outcome.row_count >= current_scenario.minimum_rows
                if outcome.expected_items is not None:
                    complete = complete and (
                        outcome.returned_items is not None
                        and outcome.returned_items >= outcome.expected_items
                    )
                sample = {
                    "round": round_number,
                    "status": "success" if complete else "incomplete",
                    "elapsedMs": round(elapsed_ms, 3),
                    "rowCount": outcome.row_count,
                    "providerCalls": outcome.provider_calls,
                }
                if outcome.expected_items is not None:
                    sample["expectedItems"] = outcome.expected_items
                    sample["returnedItems"] = outcome.returned_items
            except ProviderPermissionRequired as error:
                sample = {
                    "round": round_number,
                    "status": "permissionRequired",
                    "elapsedMs": round(
                        (time.perf_counter_ns() - call_started) / 1_000_000, 3
                    ),
                    "error": safe_error(error),
                }
            except Exception as error:  # noqa: BLE001 - 第三方异常需要进入统一结果表。
                sample = {
                    "round": round_number,
                    "status": "error",
                    "elapsedMs": round(
                        (time.perf_counter_ns() - call_started) / 1_000_000, 3
                    ),
                    "error": safe_error(error),
                }
            key = "warmups" if warmup else "samples"
            results[current_scenario_name][name][key].append(sample)

        for warmup_round in range(1, config.warmups + 1):
            order = participants.copy()
            randomizer.shuffle(order)
            for index, name in enumerate(order):
                execute(name, warmup_round, True)
                if config.interval_seconds and index < len(order) - 1:
                    sleep(config.interval_seconds)

        for measured_round in range(1, config.rounds + 1):
            order = participants.copy()
            randomizer.shuffle(order)
            for index, name in enumerate(order):
                execute(name, measured_round, False)
                is_last = measured_round == config.rounds and index == len(order) - 1
                if config.interval_seconds and not is_last:
                    sleep(config.interval_seconds)

        for name in participants:
            summary = summarize_samples(results[scenario_name][name]["samples"])
            results[scenario_name][name]["summary"] = summary
            results[scenario_name][name]["status"] = summary["status"]

    for name, provider in active.items():
        try:
            provider.close()
        except Exception as error:  # noqa: BLE001 - 继续关闭其他项目。
            providers[name]["closeError"] = safe_error(error)

    return {
        "schemaVersion": 4,
        "benchmark": "a-share-market-snapshots",
        "startedAt": started_at,
        "completedAt": utc_now(),
        "environment": {
            "operatingSystem": platform.system(),
            "osRelease": platform.release(),
            "architecture": platform.machine(),
            "processor": platform.processor() or "unknown",
            "pythonVersion": platform.python_version(),
            "logicalCpuCount": os.cpu_count(),
            "pythonImplementation": sys.implementation.name,
            "networkMode": "direct",
        },
        "configuration": {
            "symbols": [item.fqgate_code for item in config.securities],
            "providers": list(config.providers),
            "scenarios": list(config.scenarios),
            "tradeDate": config.trade_date,
            "dailyStartDate": config.daily_start_date,
            "historyCount": config.history_count,
            "rounds": config.rounds,
            "warmups": config.warmups,
            "intervalMs": round(config.interval_seconds * 1000),
            "configuredTimeoutMs": round(config.timeout_seconds * 1000),
            "randomSeed": config.seed,
        },
        "scenarios": {
            name: {
                "name": scenario.name,
                "displayName": scenario.display_name,
                "sampleDescription": scenario.sample_description,
                "minimumRows": scenario.minimum_rows,
            }
            for name, scenario in SNAPSHOT_SCENARIOS.items()
            if name in config.scenarios
        },
        "providers": providers,
        "results": results,
    }


def run_benchmark(
    config: BenchmarkConfig,
    factories: dict[str, ProviderFactory] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """在临时直连环境中执行全部快照场景。"""

    with direct_network_environment():
        return _run_benchmark(config, factories, sleep)
