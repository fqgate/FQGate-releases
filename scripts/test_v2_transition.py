"""只使用合成签名链验证静态迁移规则，不操作发布通道或客户端。"""

import copy
import hashlib
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from finalize_v2_release import (
    FRESHNESS_SIGNING_KEY_ID,
    RELEASE_SIGNING_KEY_ID,
    REQUIRED_ENTRIES,
    canonical_json,
    sign_document,
    validate_release_payload,
)
from v2_history import HistoryAudit, HistorySnapshot
from v2_transition import (
    FRESHNESS_PATH,
    ROOT,
    bind_installed_version,
    derive_security_states,
    transition_report,
)


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.release_key = Ed25519PrivateKey.generate()
        self.freshness_key = Ed25519PrivateKey.generate()
        self.heads = {"github": "a" * 40, "gitee": "b" * 40}

    def release(self, version, sequence, note="合成发行说明"):
        artifacts = []
        for platform, arch in [
            ("windows", "x86_64"),
            ("macos", "aarch64"),
            ("macos", "x86_64"),
        ]:
            name = f"FQGate-{version}-{platform}-{arch}.zip"
            artifacts.append(
                {
                    "platform": platform,
                    "arch": arch,
                    "fileName": name,
                    "installMode": "replaceApplication",
                    "updatePayload": {
                        "schemaVersion": 1,
                        "format": "zip",
                        "requiredEntries": REQUIRED_ENTRIES[platform],
                    },
                    "downloadUrls": {
                        "github": f"https://github.com/fqgate/FQGate-releases/releases/download/fqgate-v{version}/{name}",
                        "gitee": f"https://gitee.com/qicuo/fqgate-releases/attach_files/1/download/{name}",
                    },
                    "bytes": 100,
                    "sha256": "c" * 64,
                }
            )
        payload = {
            "schemaVersion": 1,
            "channel": "stable",
            "sequence": sequence,
            "version": version,
            "publishedAt": 1,
            "releaseNotes": [note],
            "artifacts": artifacts,
        }
        validate_release_payload(payload)
        return sign_document(payload, self.release_key, RELEASE_SIGNING_KEY_ID)

    def chain(
        self,
        version="2.0.0",
        sequence=1,
        refresh=1,
        *,
        releases=None,
        note="合成发行说明",
    ):
        files = dict(releases or {})
        release = self.release(version, sequence, note)
        files[f"{ROOT}/{version}/release.json"] = release
        stable = sign_document(
            {
                "schemaVersion": 1,
                "channel": "stable",
                "sequence": sequence,
                "version": version,
                "releasePath": f"{version}/release.json",
                "releaseSha256": hashlib.sha256(release).hexdigest(),
            },
            self.release_key,
            RELEASE_SIGNING_KEY_ID,
        )
        files[f"{ROOT}/{sequence}/stable.json"] = stable
        files[FRESHNESS_PATH] = sign_document(
            {
                "schemaVersion": 1,
                "channel": "stable",
                "refreshSequence": refresh,
                "stablePath": f"{sequence}/stable.json",
                "stableSha256": hashlib.sha256(stable).hexdigest(),
            },
            self.freshness_key,
            FRESHNESS_SIGNING_KEY_ID,
        )
        return files

    def history(self, snapshots, *, max_sequence=None, max_refresh=None, violations=()):
        documents = {}
        for snapshot in snapshots:
            for path, document in snapshot.files.items():
                documents.setdefault(path, set()).add(document)
        # 夹具显式描述已审计最大值，不在测试中猜测管理文档的序号。
        return HistoryAudit(
            self.heads.copy(),
            max_sequence or 1,
            max_refresh or 1,
            documents,
            list(snapshots),
            list(violations),
        )

    def snapshot(self, files, channel="github", commit="a" * 40):
        return HistorySnapshot(channel, commit, files)

    def plan(self, version="2.0.1"):
        return {
            "schemaVersion": 1,
            "mode": "manual-install-preserve-security-state",
            "targetVersion": version,
            "historyHeads": self.heads.copy(),
            "approved": True,
        }

    def test_compatible_forward_release_checks_every_source_state(self):
        old = self.chain()
        history = self.history(
            [self.snapshot(old), self.snapshot(old, "gitee", "b" * 40)]
        )
        candidate = self.chain("2.0.1", 2, 2, releases=old)
        report = transition_report(history, "2.0.1", 2, 2, candidate)
        self.assertTrue(report["allowed"])
        self.assertTrue(report["automaticAllowed"])
        self.assertEqual(report["historicalStateCount"], 2)
        self.assertEqual(len(report["stateIds"]), 2)
        for result in report["perStateAutomatic"]:
            self.assertEqual(
                result["stateId"],
                hashlib.sha256(canonical_json(result["securityState"])).hexdigest(),
            )
            self.assertEqual(
                set(result["securityState"]),
                {
                    "schemaVersion",
                    "installedVersion",
                    "trustedUnixSeconds",
                    "acceptedStable",
                    "currentRelease",
                    "sources",
                },
            )
        self.assertFalse(report["recoveryValidated"])
        self.assertFalse(report["requires_manual_upgrade"])
        self.assertIn("静态", __import__("v2_transition").__doc__)
        self.assertTrue(
            any("GUI" in limitation for limitation in report["limitations"])
        )

    def conflicting_history(self):
        first = self.chain(note="历史第一次对外提供的内容")
        second = self.chain(sequence=2, refresh=2, note="同版本路径后来被改写")
        history = self.history(
            [
                self.snapshot(first, commit="c" * 40),
                self.snapshot(second),
            ],
            max_sequence=2,
            max_refresh=2,
            violations=["历史同路径被覆盖"],
        )
        candidate = self.chain("2.0.1", 3, 3, releases=second)
        return history, candidate

    def test_new_sequence_does_not_bypass_installed_release_hash_lock(self):
        history, candidate = self.conflicting_history()
        report = transition_report(history, "2.0.1", 3, 3, candidate)
        self.assertFalse(report["allowed"])
        self.assertFalse(report["automaticAllowed"])
        self.assertTrue(report["requires_manual_upgrade"])
        self.assertEqual(len(report["currentReleaseHashConflicts"]), 1)
        self.assertEqual(
            report["perStateAutomatic"][0]["reasons"],
            ["accepted_current_release_conflict"],
        )
        self.assertTrue(report["perStateAutomatic"][1]["allowed"])
        # 默认没有人工安装许可或清除用户状态的操作。
        self.assertFalse(report["recoveryValidated"])
        self.assertEqual(report["perStateAfterVersionChange"], [])

    def test_approved_plan_still_checks_each_state_after_real_version_change(self):
        history, candidate = self.conflicting_history()
        original = copy.deepcopy(history.snapshots)
        report = transition_report(
            history, "2.0.1", 3, 3, candidate, recovery_plan=self.plan()
        )
        self.assertTrue(report["recoveryValidated"])
        self.assertTrue(report["allowed"])
        self.assertFalse(report["automaticAllowed"])
        self.assertEqual(len(report["perStateAfterVersionChange"]), 2)
        for old, migrated in zip(
            report["perStateAutomatic"], report["perStateAfterVersionChange"]
        ):
            before, after = old["securityState"], migrated["securityState"]
            self.assertEqual(after["installedVersion"], "2.0.1")
            self.assertIsNone(after["currentRelease"])
            self.assertEqual(after["acceptedStable"], before["acceptedStable"])
            for source, baseline in before["sources"].items():
                expected = {**baseline, "currentReleaseSha256": None}
                self.assertEqual(after["sources"][source], expected)
            self.assertTrue(migrated["allowed"])
        self.assertEqual(history.snapshots, original)

    def test_approval_never_overrides_reserved_historical_sequence_maximum(self):
        history, candidate = self.conflicting_history()
        history.max_sequence = 100
        history.max_refresh_sequence = 200
        report = transition_report(
            history, "2.0.1", 3, 3, candidate, recovery_plan=self.plan()
        )
        self.assertFalse(report["allowed"])
        self.assertFalse(report["recoveryValidated"])
        self.assertEqual(
            set(report["preconditionFailures"]),
            {
                "sequence_not_above_all_history",
                "refresh_sequence_not_above_all_history",
            },
        )

    def test_same_version_cannot_reset_current_release_hash_even_with_plan(self):
        first = self.chain()
        history = self.history([self.snapshot(first)])
        overwritten = self.chain("2.0.0", 2, 2, note="不同内容")
        report = transition_report(
            history, "2.0.0", 2, 2, overwritten, recovery_plan=self.plan("2.0.0")
        )
        self.assertFalse(report["allowed"])
        self.assertIn("version_not_above_all_history", report["preconditionFailures"])
        self.assertIn(
            "installed_version_unchanged",
            report["perStateAfterVersionChange"][0]["reasons"],
        )
        self.assertIsNotNone(
            report["perStateAfterVersionChange"][0]["securityState"]["currentRelease"]
        )

    def test_current_release_missing_blocks_old_clients_even_with_new_chain(self):
        old = self.chain()
        history = self.history([self.snapshot(old)])
        candidate = self.chain("2.0.1", 2, 2)
        report = transition_report(history, "2.0.1", 2, 2, candidate)
        self.assertFalse(report["allowed"])
        self.assertEqual(
            report["perStateAutomatic"][0]["reasons"], ["current_release_missing"]
        )
        self.assertTrue(report["requires_manual_upgrade"])

    def test_equal_or_lower_sequence_and_freshness_are_rejected(self):
        old = self.chain(sequence=3, refresh=4)
        history = self.history([self.snapshot(old)], max_sequence=3, max_refresh=4)
        for sequence, refresh, expected in [
            (
                3,
                4,
                [
                    "source_stable_equivocation:github",
                    "source_freshness_equivocation:github",
                    "accepted_stable_equivocation",
                ],
            ),
            (
                2,
                3,
                [
                    "source_stable_replay:github",
                    "source_freshness_replay:github",
                    "accepted_stable_replay",
                ],
            ),
        ]:
            candidate = self.chain("2.0.1", sequence, refresh, releases=old)
            report = transition_report(history, "2.0.1", sequence, refresh, candidate)
            self.assertFalse(report["allowed"])
            self.assertEqual(report["perStateAutomatic"][0]["reasons"], expected)

    def test_invalid_or_stale_plan_is_rejected_instead_of_treating_it_as_approval(self):
        history, candidate = self.conflicting_history()
        for changes in [
            {"approved": False},
            {"approved": 1},
            {"schemaVersion": True},
            {"targetVersion": "2.0.2"},
            {"historyHeads": {"github": "f" * 40}},
            {"mode": "clear-user-state"},
            {"deleteState": True},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                transition_report(
                    history,
                    "2.0.1",
                    3,
                    3,
                    candidate,
                    recovery_plan={**self.plan(), **changes},
                )

    def test_version_binding_preserves_clock_and_all_non_current_baselines(self):
        history = self.history([self.snapshot(self.chain())])
        before = derive_security_states(history)[0]["securityState"]
        before["trustedUnixSeconds"] = 12345
        before["currentRelease"]["expiresAt"] = 45678
        before["sources"]["gitee"] = copy.deepcopy(before["sources"]["github"])
        original = copy.deepcopy(before)
        same = bind_installed_version(before, "2.0.0")
        self.assertEqual(same, original)
        changed = bind_installed_version(before, "2.0.1")
        self.assertEqual(changed["trustedUnixSeconds"], 12345)
        self.assertEqual(changed["acceptedStable"], original["acceptedStable"])
        self.assertEqual(set(changed["sources"]), {"github", "gitee"})
        for source, baseline in original["sources"].items():
            self.assertEqual(
                changed["sources"][source], {**baseline, "currentReleaseSha256": None}
            )
        self.assertEqual(before, original)

    def test_no_complete_history_never_fabricates_a_previous_client_state(self):
        release_path = f"{ROOT}/2.0.0/release.json"
        partial = {release_path: self.release("2.0.0", 1)}
        history = self.history([self.snapshot(partial)], violations=["历史断链"])
        report = transition_report(history, "2.0.1", 2, 2, self.chain("2.0.1", 2, 2))
        self.assertFalse(report["allowed"])
        self.assertEqual(report["stateIds"], [])
        self.assertIn(
            "no_complete_verified_historical_chain", report["preconditionFailures"]
        )
        self.assertEqual(report["historicalStateCount"], 0)

    def test_genesis_has_no_cached_state_but_remains_only_static_validation(self):
        history = HistoryAudit(self.heads.copy(), 0, 0, {}, [], [])
        report = transition_report(history, "2.0.0", 1, 1, self.chain())
        self.assertTrue(report["allowed"])
        self.assertEqual(report["stateIds"], [])
        self.assertEqual(report["simulation"], "static_update_security_transition")
        self.assertTrue(
            any("真实启动验收" in limitation for limitation in report["limitations"])
        )

    def test_state_ids_deduplicate_snapshot_provenance_not_security_baselines(self):
        old = self.chain()
        history = self.history(
            [self.snapshot(old, commit="c" * 40), self.snapshot(old)]
        )
        report = transition_report(
            history, "2.0.1", 2, 2, self.chain("2.0.1", 2, 2, releases=old)
        )
        self.assertEqual(len(report["perStateAutomatic"]), 1)
        self.assertEqual(report["historicalStateCount"], 1)
        self.assertEqual(report["historicalObservationCount"], 2)
        self.assertEqual(len(report["stateIds"]), 1)
        self.assertEqual(
            report["perStateAutomatic"][0]["provenance"],
            [
                {"channel": "github", "commit": "c" * 40},
                {"channel": "github", "commit": "a" * 40},
            ],
        )

    def test_duplicate_snapshot_references_do_not_duplicate_provenance(self):
        old = self.chain()
        history = self.history([self.snapshot(old), self.snapshot(old)])
        report = transition_report(
            history, "2.0.1", 2, 2, self.chain("2.0.1", 2, 2, releases=old)
        )
        self.assertEqual(report["historicalStateCount"], 1)
        self.assertEqual(report["historicalObservationCount"], 1)
        self.assertEqual(
            report["perStateAutomatic"][0]["provenance"],
            [
                {"channel": "github", "commit": "a" * 40},
            ],
        )

    def test_recovery_keeps_all_provenance_without_duplicating_security_state(self):
        old = self.chain()
        history = self.history(
            [self.snapshot(old, commit="c" * 40), self.snapshot(old)]
        )
        report = transition_report(
            history,
            "2.0.1",
            2,
            2,
            self.chain("2.0.1", 2, 2, releases=old),
            recovery_plan=self.plan(),
        )
        self.assertTrue(report["recoveryValidated"])
        self.assertEqual(len(report["perStateAfterVersionChange"]), 1)
        self.assertEqual(
            report["perStateAfterVersionChange"][0]["provenance"],
            report["perStateAutomatic"][0]["provenance"],
        )

    def test_future_current_release_with_old_latest_remains_a_distinct_state(self):
        old = self.chain()
        preuploaded = {**old, f"{ROOT}/2.0.1/release.json": self.release("2.0.1", 2)}
        history = self.history(
            [self.snapshot(old, commit="c" * 40), self.snapshot(preuploaded)]
        )
        candidate = self.chain("2.0.2", 3, 3, releases=preuploaded)
        report = transition_report(history, "2.0.2", 3, 3, candidate)
        self.assertTrue(report["allowed"])
        self.assertEqual(report["historicalStateCount"], 2)
        self.assertEqual(report["historicalObservationCount"], 3)
        future = next(
            result
            for result in report["perStateAutomatic"]
            if result["securityState"]["installedVersion"] == "2.0.1"
        )
        self.assertEqual(future["securityState"]["acceptedStable"]["version"], "2.0.0")
        self.assertEqual(
            future["securityState"]["currentRelease"]["releaseSha256"],
            hashlib.sha256(preuploaded[f"{ROOT}/2.0.1/release.json"]).hexdigest(),
        )
        self.assertEqual(
            future["provenance"], [{"channel": "github", "commit": "a" * 40}]
        )

    def test_management_documents_are_not_installed_release_candidates(self):
        old = self.chain()
        old[f"{ROOT}/publications/2.0.9/intent.json"] = sign_document(
            {
                "schemaVersion": 1,
                "kind": "intent",
                "version": "2.0.9",
            },
            self.release_key,
            RELEASE_SIGNING_KEY_ID,
        )
        history = self.history([self.snapshot(old)])
        report = transition_report(
            history, "2.0.1", 2, 2, self.chain("2.0.1", 2, 2, releases=old)
        )
        self.assertEqual(report["historicalStateCount"], 1)
        self.assertTrue(report["allowed"])

    def test_candidate_preview_must_match_parameters_and_hash_bindings(self):
        history = self.history([self.snapshot(self.chain())])
        candidate = self.chain("2.0.1", 2, 2)
        for version, sequence, refresh in [
            ("2.0.2", 2, 2),
            ("2.0.1", 3, 2),
            ("2.0.1", 2, 3),
            ("2.0.1", True, 2),
        ]:
            with self.subTest(
                version=version, sequence=sequence, refresh=refresh
            ), self.assertRaises(ValueError):
                transition_report(history, version, sequence, refresh, candidate)
        candidate[f"{ROOT}/2.0.1/release.json"] = self.release("2.0.1", 2, "不同文档")
        with self.assertRaises(ValueError):
            transition_report(history, "2.0.1", 2, 2, candidate)

    def test_reserved_resume_accepts_equal_maxima_only_with_identical_target_chain(
        self,
    ):
        old = self.chain()
        candidate = self.chain("2.0.1", 2, 2, releases=old)
        history = self.history(
            [self.snapshot(old, commit="c" * 40), self.snapshot(candidate)],
            max_sequence=2,
            max_refresh=2,
        )
        ordinary = transition_report(history, "2.0.1", 2, 2, candidate)
        self.assertFalse(ordinary["allowed"])
        resumed = transition_report(
            history, "2.0.1", 2, 2, candidate, allow_reserved_sequence=True
        )
        self.assertTrue(resumed["allowed"])
        self.assertTrue(resumed["reservedSequenceMode"])
        self.assertTrue(all(state["allowed"] for state in resumed["perStateAutomatic"]))

    def test_reserved_resume_does_not_allow_same_version_different_bytes(self):
        previous = self.chain("2.0.1", 2, 2)
        history = self.history([self.snapshot(previous)], max_sequence=2, max_refresh=2)
        different = self.chain("2.0.1", 2, 2, note="不属于原 intent 的不同文档")
        report = transition_report(
            history, "2.0.1", 2, 2, different, allow_reserved_sequence=True
        )
        self.assertFalse(report["allowed"])
        self.assertIn("version_not_above_all_history", report["preconditionFailures"])
        self.assertIn(
            "source_stable_equivocation:github",
            report["perStateAutomatic"][0]["reasons"],
        )

    def test_reserved_resume_cannot_go_below_any_historical_maximum(self):
        old = self.chain()
        history = self.history([self.snapshot(old)], max_sequence=10, max_refresh=20)
        candidate = self.chain("2.0.1", 2, 2, releases=old)
        report = transition_report(
            history, "2.0.1", 2, 2, candidate, allow_reserved_sequence=True
        )
        self.assertFalse(report["allowed"])
        self.assertEqual(
            set(report["preconditionFailures"]),
            {
                "sequence_not_above_all_history",
                "refresh_sequence_not_above_all_history",
            },
        )

    def test_genesis_reserved_resume_has_no_fabricated_cached_state(self):
        candidate = self.chain()
        reserved = {
            path: value for path, value in candidate.items() if path != FRESHNESS_PATH
        }
        history = self.history([self.snapshot(reserved)])
        report = transition_report(
            history, "2.0.0", 1, 1, candidate, allow_reserved_sequence=True
        )
        self.assertTrue(report["allowed"])
        self.assertEqual(report["stateIds"], [])
        reserved[f"{ROOT}/2.0.0/release.json"] = self.release(
            "2.0.0", 1, "不一致旧文档"
        )
        polluted = self.history([self.snapshot(reserved)], violations=["摘要不匹配"])
        blocked = transition_report(
            polluted, "2.0.0", 1, 1, candidate, allow_reserved_sequence=True
        )
        self.assertFalse(blocked["allowed"])
        self.assertIn(
            "no_complete_verified_historical_chain", blocked["preconditionFailures"]
        )


if __name__ == "__main__":
    unittest.main()
