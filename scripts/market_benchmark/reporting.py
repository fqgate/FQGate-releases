"""从原始测试记录生成面向使用者、不带排名的独立接口表格。"""

from __future__ import annotations

import html
import json
from pathlib import Path

STATUS_LABELS = {
    "completed": "**✅ 可用**",
    "unsupported": "**— 不支持**",
    "unavailable": "**❌ 不可用**",
    "permissionRequired": "**🔒 需要权限**",
    "failed": "**❌ 失败**",
    "incomplete": "**⚠️ 返回不完整**",
}


def format_number(value: object, digits: int = 3) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:.{digits}f}"


def _markdown_cell(value: object) -> str:
    """转义表格控制字符；Markdown 表格内不混入 HTML 标签。"""

    escaped = html.escape(str(value), quote=False).replace("|", "\\|")
    return " ".join(escaped.split())


def _success_rate(summary: dict) -> str:
    attempts = summary["attemptCount"]
    successes = summary["successCount"]
    if not attempts:
        return "—"
    return f"**{successes}/{attempts}（{successes / attempts:.0%}）**"


def _returned_data(summary: dict) -> str:
    coverage = summary.get("itemCoverage")
    if coverage:
        expected = coverage["expected"]
        minimum = coverage["returnedMinimum"]
        maximum = coverage["returnedMaximum"]
        returned = str(minimum) if minimum == maximum else f"{minimum}–{maximum}"
        return f"**{returned}/{expected} 个目标**"

    rows = summary.get("rowCount")
    if not rows:
        return "—"
    minimum = rows["minimum"]
    maximum = rows["maximum"]
    if minimum == maximum:
        return f"**{format_number(minimum, 0)} 条**"
    return (
        f"**{format_number(minimum, 0)}–{format_number(maximum, 0)} 条**"
        f"；平均 {format_number(rows['mean'], 1)} 条"
    )


def _latency(summary: dict, percentile: str, emphasize: bool = False) -> str:
    latency = summary.get("latencyMs") or {}
    value = latency.get(percentile)
    if not isinstance(value, (int, float)):
        return "—"
    rendered = f"{format_number(value)} ms"
    return f"**{rendered}**" if emphasize else rendered


def _problem_details(provider: dict, state: dict) -> str:
    """Markdown 只列错误类型和次数，具体响应留在 JSON 测试记录。"""

    groups: dict[str, dict[str, int]] = {}
    for phase_key, phase_name in (("samples", "正式"), ("warmups", "预热")):
        for sample in state.get(phase_key, []):
            error = sample.get("error")
            if not error:
                continue
            counts = groups.setdefault(error["type"], {})
            counts[phase_name] = counts.get(phase_name, 0) + 1

    details: list[str] = []
    for error_type, counts in groups.items():
        occurrences = "、".join(f"{name} {count} 次" for name, count in counts.items())
        details.append(f"{_markdown_cell(error_type)}（{occurrences}）")

    setup_error = provider.get("setupError")
    if setup_error:
        details.append(f"项目准备：{_markdown_cell(setup_error['type'])}")
    close_error = provider.get("closeError")
    if close_error:
        details.append(f"项目关闭：{_markdown_cell(close_error['type'])}")

    if details:
        return "；".join(details)
    return "—"


def _software_versions(report: dict) -> str:
    versions = []
    for provider_name in report["configuration"]["providers"]:
        metadata = report["providers"][provider_name]["metadata"]
        version = metadata.get("version") or "未知"
        versions.append(f"{metadata['displayName']} {version}")
    return _markdown_cell("；".join(versions))


def render_markdown(report: dict) -> str:
    config = report["configuration"]
    lines = [
        "# A 股行情快照接口测试结果",
        "",
        "| 参数 | 值 |",
        "| --- | --- |",
        f"| 测试时间 | `{report['startedAt']}` 至 `{report['completedAt']}` |",
        f"| 证券 | `{', '.join(config['symbols'])}` |",
        f"| 数据交易日 | `{config['tradeDate']}` |",
        f"| 正式 / 预热轮次 | `{config['rounds']} / {config['warmups']}` |",
        f"| 软件版本 | {_software_versions(report)} |",
    ]
    for scenario_name in config["scenarios"]:
        scenario = report["scenarios"][scenario_name]
        lines.extend(
            [
                "",
                f"## {scenario['displayName']}",
                "",
                f"样本：{scenario['sampleDescription']}",
                "",
                "| 项目 | 结果 | 成功率 | 返回数据 | P95 慢请求 | P50 典型请求 | 错误 |",
                "| --- | --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for provider_name in config["providers"]:
            provider = report["providers"][provider_name]
            metadata = provider["metadata"]
            state = report["results"][scenario_name][provider_name]
            summary = state["summary"]
            project = f"[{metadata['displayName']}]({metadata['projectUrl']})"
            lines.append(
                f"| {project} | "
                f"{STATUS_LABELS.get(summary['status'], summary['status'])} | "
                f"{_success_rate(summary)} | "
                f"{_returned_data(summary)} | "
                f"{_latency(summary, 'p95', emphasize=True)} | "
                f"{_latency(summary, 'p50')} | "
                f"{_problem_details(provider, state)} |"
            )
    lines.append("")
    return "\n".join(lines)


def write_reports(report: dict, output: Path) -> tuple[Path, Path]:
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path = (
        output if output.suffix.lower() == ".json" else output.with_suffix(".json")
    )
    markdown_path = json_path.with_suffix(".md")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_markdown_report(report, markdown_path)
    return json_path, markdown_path


def write_markdown_report(report: dict, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(report), encoding="utf-8")
    return output


def generate_markdown_report(test_record: Path, output: Path | None = None) -> Path:
    """从 JSON 测试记录重新生成 Markdown 报告。"""

    try:
        report = json.loads(test_record.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON 测试记录格式错误：{error}") from error
    if (
        not isinstance(report, dict)
        or report.get("benchmark") != "a-share-market-snapshots"
    ):
        raise ValueError("不是 A 股行情快照测试记录")
    required = {"configuration", "scenarios", "providers", "results"}
    missing = sorted(required - report.keys())
    if missing:
        raise ValueError(f"JSON 测试记录缺少字段：{', '.join(missing)}")
    return write_markdown_report(report, output or test_record.with_suffix(".md"))
