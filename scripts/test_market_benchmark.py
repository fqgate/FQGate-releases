import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from market_benchmark.calendar import latest_completed_trade_date
from market_benchmark.models import (
    FetchResult,
    Security,
    SnapshotContext,
    parse_securities,
)
from market_benchmark.providers import (
    FqgateProvider,
    MarketProvider,
    ProviderMetadata,
    ProviderPermissionRequired,
)
from market_benchmark.reporting import (
    generate_markdown_report,
    render_markdown,
    write_reports,
)
from market_benchmark.runner import (
    BenchmarkConfig,
    nearest_rank,
    run_benchmark,
    safe_error,
)


class FakeProvider(MarketProvider):
    def __init__(
        self,
        _base_url,
        _timeout,
        name="fake",
        supported=("selected_quote",),
        fail=False,
        permission=False,
    ):
        self.metadata = ProviderMetadata(
            name,
            name,
            "https://example.test",
            supported,
            "1.0.0",
        )
        self.fail = fail
        self.permission = permission

    def setup(self):
        pass

    def fetch(self, _scenario, context):
        if self.permission:
            raise ProviderPermissionRequired("测试权限不足")
        if self.fail:
            raise TimeoutError("测试超时")
        return FetchResult(
            len(context.securities),
            1,
            len(context.securities),
            len(context.securities),
        )


def make_config(**overrides):
    values = {
        "securities": (Security("USHA", "600519"),),
        "providers": ("fake",),
        "scenarios": ("selected_quote",),
        "trade_date": "20260911",
        "daily_start_date": "20250906",
        "rounds": 2,
        "warmups": 0,
        "interval_seconds": 0,
    }
    values.update(overrides)
    return BenchmarkConfig(**values)


class MarketBenchmarkTests(unittest.TestCase):
    def test_security_parser_accepts_common_prefixes(self):
        self.assertEqual(
            parse_securities("600519,SZ000001,BJ:430047"),
            (
                Security("USHA", "600519"),
                Security("USZA", "000001"),
                Security("USBJ", "430047"),
            ),
        )

    def test_security_parser_rejects_duplicates(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            parse_securities("600519,SH600519")

    def test_security_parser_rejects_mismatched_market(self):
        with self.assertRaisesRegex(ValueError, "不匹配"):
            parse_securities("SZ600519")

    def test_nearest_rank_uses_observed_value(self):
        self.assertEqual(nearest_rank([1.0, 2.0, 3.0, 4.0], 0.95), 4.0)

    def test_error_summary_keeps_full_detail_but_redacts_credentials(self):
        detail = "接口原始错误详情：" + "x" * 500
        summary = safe_error(
            RuntimeError(
                "访问 https://name:secret@example.test/path?symbol=600519&token=private "
                "失败 password=hidden\n"
                "Authorization: Bearer auth-secret\n"
                "Cookie: sid=cookie-secret; theme=dark\n"
                f"{detail}"
            )
        )
        self.assertNotIn("secret", summary["message"])
        self.assertNotIn("private", summary["message"])
        self.assertNotIn("hidden", summary["message"])
        self.assertNotIn("auth-secret", summary["message"])
        self.assertNotIn("cookie-secret", summary["message"])
        self.assertIn("symbol=600519", summary["message"])
        self.assertIn("Authorization: ***", summary["message"])
        self.assertIn("Cookie: ***", summary["message"])
        self.assertIn(detail, summary["message"])
        self.assertIn("\n", summary["message"])

    def test_fqgate_provider_rejects_credentials_in_url(self):
        with self.assertRaisesRegex(ValueError, "用户名"):
            FqgateProvider("http://name:secret@127.0.0.1:17281", 2)

    def test_runner_lists_results_without_ranking(self):
        factories = {
            "complete": lambda base, timeout: FakeProvider(base, timeout, "complete"),
            "unsupported": lambda base, timeout: FakeProvider(
                base, timeout, "unsupported", supported=()
            ),
        }
        report = run_benchmark(
            make_config(providers=("complete", "unsupported")),
            factories=factories,
            sleep=lambda _seconds: None,
        )
        self.assertNotIn("ranking", report)
        self.assertEqual(
            report["results"]["selected_quote"]["complete"]["status"],
            "completed",
        )
        self.assertEqual(
            report["results"]["selected_quote"]["unsupported"]["status"],
            "unsupported",
        )

    def test_runner_distinguishes_permission_and_failure(self):
        factories = {
            "permission": lambda base, timeout: FakeProvider(
                base, timeout, "permission", permission=True
            ),
            "failure": lambda base, timeout: FakeProvider(
                base, timeout, "failure", fail=True
            ),
        }
        report = run_benchmark(
            make_config(providers=("permission", "failure")),
            factories=factories,
            sleep=lambda _seconds: None,
        )
        states = report["results"]["selected_quote"]
        self.assertEqual(states["permission"]["status"], "permissionRequired")
        self.assertEqual(states["failure"]["status"], "failed")

    def test_runner_forces_direct_network_and_restores_environment(self):
        observations = []

        class EnvironmentProvider(FakeProvider):
            def fetch(self, scenario, context):
                observations.append(
                    {
                        "httpProxy": os.environ.get("HTTP_PROXY"),
                        "httpsProxy": os.environ.get("HTTPS_PROXY"),
                        "allProxy": os.environ.get("ALL_PROXY"),
                        "noProxy": os.environ.get("NO_PROXY"),
                    }
                )
                return super().fetch(scenario, context)

        original_proxy = "http://proxy.example.test:8080"
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": original_proxy,
                "HTTPS_PROXY": original_proxy,
                "ALL_PROXY": original_proxy,
                "NO_PROXY": "localhost",
            },
            clear=False,
        ):
            report = run_benchmark(
                make_config(rounds=1),
                factories={"fake": EnvironmentProvider},
                sleep=lambda _seconds: None,
            )
            self.assertEqual(os.environ.get("HTTP_PROXY"), original_proxy)
            self.assertEqual(os.environ.get("NO_PROXY"), "localhost")

        self.assertEqual(
            observations,
            [
                {
                    "httpProxy": None,
                    "httpsProxy": None,
                    "allProxy": None,
                    "noProxy": "*",
                }
            ],
        )
        self.assertEqual(report["environment"]["networkMode"], "direct")

    def test_report_is_table_only_and_omits_market_values(self):
        report = run_benchmark(
            make_config(rounds=1),
            factories={"fake": FakeProvider},
            sleep=lambda _seconds: None,
        )
        markdown = render_markdown(report)
        self.assertIn("## 指定证券行情快照", markdown)
        self.assertNotIn("用户关注", markdown)
        self.assertIn("| 项目 | 结果 | 成功率 | 返回数据 | P95 慢请求 |", markdown)
        self.assertIn("**1/1（100%）**", markdown)
        self.assertIn("**1/1 个目标**", markdown)
        self.assertIn("| 软件版本 | fake 1.0.0 |", markdown)
        self.assertEqual(markdown.count("1.0.0"), 1)
        self.assertNotIn("初始化（ms）", markdown)
        self.assertNotRegex(markdown, r"<[A-Za-z/]")
        self.assertNotIn("排名", markdown)
        self.assertNotIn("price", markdown.lower())
        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path = write_reports(
                report, Path(directory) / "result.json"
            )
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            regenerated_path = generate_markdown_report(
                json_path, Path(directory) / "regenerated.md"
            )
            self.assertEqual(
                markdown_path.read_text(encoding="utf-8"),
                regenerated_path.read_text(encoding="utf-8"),
            )

    def test_report_uses_one_table_per_scenario(self):
        report = run_benchmark(
            make_config(
                rounds=1,
                scenarios=("selected_quote", "market_snapshot"),
            ),
            factories={"fake": FakeProvider},
            sleep=lambda _seconds: None,
        )
        markdown = render_markdown(report)
        self.assertEqual(markdown.count("| 项目 | 结果 | 成功率 |"), 2)
        self.assertIn("## 指定证券行情快照", markdown)
        self.assertIn("## A 股全市场快照", markdown)

    def test_report_only_lists_error_types_and_groups_counts(self):
        class ErrorProvider(FakeProvider):
            def __init__(self, base_url, timeout):
                super().__init__(base_url, timeout)
                self.calls = 0

            def fetch(self, _scenario, _context):
                self.calls += 1
                if self.calls < 3:
                    raise RuntimeError("第一类错误\n完整详情")
                raise ValueError("第二类错误")

        report = run_benchmark(
            make_config(rounds=3),
            factories={"fake": ErrorProvider},
            sleep=lambda _seconds: None,
        )
        markdown = render_markdown(report)
        self.assertIn("RuntimeError（正式 2 次）", markdown)
        self.assertIn("ValueError（正式 1 次）", markdown)
        self.assertNotIn("第一类错误", markdown)
        self.assertNotIn("第二类错误", markdown)

    def test_report_moves_large_response_payload_to_json_record(self):
        payload = '[{"symbol":"bj920992","trade":"11.290"}]'

        class PayloadErrorProvider(FakeProvider):
            def fetch(self, _scenario, _context):
                raise FileNotFoundError(f"File {payload} does not exist")

        report = run_benchmark(
            make_config(rounds=1),
            factories={"fake": PayloadErrorProvider},
            sleep=lambda _seconds: None,
        )
        markdown = render_markdown(report)
        raw_message = report["results"]["selected_quote"]["fake"]["samples"][0][
            "error"
        ]["message"]
        self.assertIn(payload, raw_message)
        self.assertNotIn(payload, markdown)
        self.assertIn("FileNotFoundError（正式 1 次）", markdown)
        self.assertNotIn("接口响应内容被作为文件路径读取", markdown)
        self.assertNotIn("详见 JSON 测试记录", markdown)

    def test_fqgate_provider_queries_and_checks_identity(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def reply(self, document, status=200):
                content = json.dumps(document).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                if self.path == "/openapi.json":
                    self.reply({"info": {"version": "0.2.0"}})
                else:
                    self.reply({"code": 0, "data": {"status": "ok"}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                rows = [
                    {"5": {"value": item["market"] + item["code"]}}
                    for item in body["securities"]
                ]
                self.reply(
                    {
                        "code": 0,
                        "data": {"records": [rows], "row_count": len(rows)},
                    }
                )

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        provider = FqgateProvider(f"http://127.0.0.1:{server.server_port}", 2)
        context = SnapshotContext(
            (Security("USHA", "600519"), Security("USZA", "000001")),
            "20260911",
            "20250906",
            240,
        )
        try:
            provider.setup()
            result = provider.fetch("selected_quote", context)
        finally:
            provider.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(result.row_count, 2)
        self.assertEqual(result.provider_calls, 2)
        self.assertEqual(result.returned_items, 2)

    def test_latest_completed_trade_date_uses_calendar_result(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                content = json.dumps(
                    {
                        "code": 0,
                        "data": {"records": ["20260910", "20260911"]},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = latest_completed_trade_date(
                f"http://127.0.0.1:{server.server_port}", 2
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(result, "20260911")


if __name__ == "__main__":
    unittest.main()
