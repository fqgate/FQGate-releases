import unittest
from pathlib import Path

import finalize_release
import finalize_v2_release


class ReleaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.repository_root = Path(__file__).parents[1]

    def test_workflows_use_distinct_events_and_publishers(self):
        legacy = (
            self.repository_root / ".github/workflows/finalize-release.yml"
        ).read_text(encoding="utf-8")
        v2 = (
            self.repository_root / ".github/workflows/finalize-v2-release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("types: [fqgate-draft-ready]", legacy)
        self.assertIn("scripts/finalize_release.py", legacy)
        self.assertNotIn("fqgate-v2-draft-ready", legacy)
        self.assertNotIn("finalize_v2_release.py", legacy)

        self.assertIn("types: [fqgate-v2-draft-ready]", v2)
        self.assertIn("scripts/finalize_v2_release.py", v2)
        self.assertNotIn("types: [fqgate-draft-ready]", v2)

    def test_publishers_keep_independent_contracts_and_roots(self):
        self.assertEqual(
            finalize_release.EXPECTED_TARGETS,
            {
                ("windows", "x86_64", "replaceExecutable"),
                ("macos", "aarch64", "openPackage"),
                ("macos", "x86_64", "openPackage"),
            },
        )
        self.assertEqual(
            finalize_v2_release.EXPECTED_TARGETS,
            {
                ("windows", "x86_64", "replaceApplication"),
                ("macos", "aarch64", "replaceApplication"),
                ("macos", "x86_64", "replaceApplication"),
            },
        )
        self.assertEqual(
            finalize_v2_release.ROOT_FRESHNESS_PATH,
            "releases/v2/freshness.json",
        )
        self.assertFalse(hasattr(finalize_v2_release, "ROOT_STABLE_ALIAS_PATH"))

    def test_v2_workflow_and_documentation_describe_forward_only_acceptance(self):
        workflow = (
            self.repository_root / ".github/workflows/finalize-v2-release.yml"
        ).read_text(encoding="utf-8")
        maintenance = (self.repository_root / "发布维护.md").read_text(encoding="utf-8")
        self.assertIn("finalize_v2_release.py audit", workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("group: fqgate-v2-stable-channel", workflow)
        self.assertIn("metadata_activated_pending_runtime_acceptance", maintenance)
        self.assertIn("finalize_v2_release.py accept", maintenance)
        for document in (workflow, maintenance):
            self.assertNotIn("事务切换", document)
            self.assertNotIn("原子切换", document)
            self.assertNotIn("12 小时", document)
            self.assertNotIn("每 4 小时", document)

    def test_v2_metadata_cannot_be_deleted_by_publisher(self):
        self.assertFalse(hasattr(finalize_v2_release, "restore_content"))
        self.assertFalse(hasattr(finalize_v2_release.GitHubApi, "delete_content"))
        self.assertFalse(hasattr(finalize_v2_release.GiteeApi, "delete_content"))


if __name__ == "__main__":
    unittest.main()
