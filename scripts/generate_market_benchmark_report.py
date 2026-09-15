#!/usr/bin/env python3
"""从行情快照 JSON 测试记录自动生成 Markdown 报告。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from market_benchmark.reporting import generate_markdown_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("test_record", type=Path, help="基准测试生成的 JSON 记录")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Markdown 输出路径；默认与 JSON 同名",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        output = generate_markdown_report(args.test_record, args.output)
    except (OSError, ValueError) as error:
        print(f"生成行情测试报告失败：{error}", file=sys.stderr)
        return 2
    print(f"Markdown 报告：{output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
