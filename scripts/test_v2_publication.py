"""离线发布故障注入；假资产、假托管源及假验收报告，不运行真实客户端。"""

import json
import io
import unittest
import zipfile
from unittest.mock import patch

import finalize_v2_release as protocol
import test_finalize_v2_release as fixtures
import v2_publication as publication


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.builder = fixtures.FinalizeReleaseTests()
        self.release_key, self.release_public = fixtures.key_pair()
        self.freshness_key, self.freshness_public = fixtures.key_pair()
        self.github = self.builder.create_github()
        self.gitee = fixtures.FakeGitee()

    def arguments(self, version="2.0.0"):
        return dict(
            github=self.github,
            gitee=self.gitee,
            version=version,
            release_private_key=self.release_key,
            freshness_private_key=self.freshness_key,
            release_public_key=self.release_public,
            freshness_public_key=self.freshness_public,
        )

    def finalize(self, version="2.0.0"):
        return protocol.finalize_release(**self.arguments(version))

    def acceptance(self, result):
        return {
            "schemaVersion": 1,
            "version": result["version"],
            "candidateSha256": result["candidateSha256"],
            "intentSha256": result["intentSha256"],
            "sequence": result["sequence"],
            "refreshSequence": result["refreshSequence"],
            "cases": [
                {
                    "platform": platform,
                    "architecture": architecture,
                    "stateId": state,
                    "processRunning": True,
                    "coreRunning": True,
                    "updateCheckCompleted": True,
                    "automaticExit": False,
                    "securityStatePreserved": True,
                    "evidenceSha256": "a" * 64,
                }
                for platform, architecture in (
                    ("windows", "x86_64"),
                    ("macos", "aarch64"),
                    ("macos", "x86_64"),
                )
                for state in ["fresh", *result["stateIds"]]
            ],
        }

    def accept(self, result, report=None):
        arguments = self.arguments(result["version"])
        arguments.pop("freshness_private_key")
        return publication.accept_publication(
            **arguments, report=self.acceptance(result) if report is None else report
        )

    def advance_candidate(self, version):
        upcoming = self.builder.create_github(version=version)
        self.github.release = upcoming.release
        self.github.asset_contents = upcoming.asset_contents
        self.gitee.release = None
        self.gitee.asset_contents = {}
        self.gitee.asset_records = {}

    def external_chain(self, version="2.0.0", sequence=100):
        candidate = protocol.build_candidate(
            self.builder.create_github(version=version), self.github.repository, version
        )
        return publication.freeze_documents(
            candidate,
            publication.preview_mirrored(candidate),
            sequence,
            sequence,
            1_700_000_000,
            self.release_key,
            self.freshness_key,
        )

    def inject_external_documents(self, documents):
        for channel in (self.github, self.gitee):
            for path, raw in documents.items():
                channel.compare_and_write(
                    path, raw, channel.read_content(path), "external mutation fixture"
                )

    def replace_candidate_zip_consistently(self):
        name = "FQGate-2.0.0-windows-x64.zip"
        output = io.BytesIO()
        with zipfile.ZipFile(
            io.BytesIO(self.github.asset_contents[name])
        ) as original, zipfile.ZipFile(output, "w") as changed:
            for entry in original.namelist():
                raw = original.read(entry)
                changed.writestr(
                    entry, raw + b"-different" if entry == "FQGate.exe" else raw
                )
        self.github.asset_contents[name] = output.getvalue()
        checksum = name + ".sha256"
        self.github.asset_contents[checksum] = (
            f"{protocol.sha256_bytes(output.getvalue())}  {name}\n".encode()
        )
        metadata_name = "FQGate-2.0.0-windows-x64.release.json"
        metadata = json.loads(self.github.asset_contents[metadata_name])
        for item in [metadata["package"], *metadata["assets"]]:
            if item["fileName"] in (name, checksum):
                item["size"] = len(self.github.asset_contents[item["fileName"]])
                item["sha256"] = protocol.sha256_bytes(
                    self.github.asset_contents[item["fileName"]]
                )
        self.github.asset_contents[metadata_name] = fixtures.encoded(metadata)
        for asset in self.github.release["assets"]:
            raw = self.github.asset_contents[asset["name"]]
            asset.update(size=len(raw), digest=f"sha256:{protocol.sha256_bytes(raw)}")

    def test_genesis_waits_for_real_runtime_report_before_completion(self):
        result = self.finalize()
        self.assertEqual(
            result["status"], "metadata_activated_pending_runtime_acceptance"
        )
        self.assertIsNone(
            self.github.read_content(
                publication.publication_path("2.0.0", "completion")
            )
        )
        self.assertEqual(self.accept(result)["status"], "published")
        writes = len(self.github.writes) + len(self.gitee.writes)
        self.assertEqual(self.finalize()["status"], "published")
        self.assertEqual(writes, len(self.github.writes) + len(self.gitee.writes))

    def test_faults_at_each_immutable_stage_resume_exact_same_bytes(self):
        paths = [
            publication.IN_FLIGHT_PATH,
            publication.publication_path("2.0.0", "reservation"),
            publication.publication_path("2.0.0", "intent"),
            "releases/v2/2.0.0/release.json",
            "releases/v2/1/stable.json",
            protocol.ROOT_FRESHNESS_PATH,
        ]
        for path in paths:
            for failed_side in ("github", "gitee"):
                with self.subTest(path=path, failed_side=failed_side):
                    self.setUp()
                    channel = getattr(self, failed_side)
                    channel.fail_write_once.add(path)
                    with self.assertRaisesRegex(RuntimeError, "write failed"):
                        self.finalize()
                    retained = {
                        name: dict(getattr(self, name).contents)
                        for name in ("github", "gitee")
                    }
                    result = self.finalize()
                    self.assertEqual(
                        (result["sequence"], result["refreshSequence"]), (1, 1)
                    )
                    self.assertEqual(self.github.contents, self.gitee.contents)
                    for name, documents in retained.items():
                        for retained_path, content in documents.items():
                            self.assertEqual(
                                getattr(self, name).contents[retained_path], content
                            )

    def test_unknown_commit_response_is_read_back_without_rollback(self):
        self.github.fail_after_write_once.update(
            {publication.IN_FLIGHT_PATH, protocol.ROOT_FRESHNESS_PATH}
        )
        result = self.finalize()
        self.assertEqual(result["sequence"], 1)
        self.assertEqual(self.github.contents, self.gitee.contents)

    def test_accept_completion_half_write_resumes_same_report(self):
        result = self.finalize()
        completion_path = publication.publication_path("2.0.0", "completion")
        self.github.fail_write_once.add(completion_path)
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            self.accept(result)
        retained = self.gitee.contents[completion_path]
        self.assertEqual(self.accept(result)["status"], "published")
        self.assertEqual(self.github.contents[completion_path], retained)

    def test_another_version_cannot_bypass_pending_acceptance(self):
        self.finalize()
        self.advance_candidate("2.0.1")
        writes = len(self.github.writes) + len(self.gitee.writes)
        with self.assertRaisesRegex(RuntimeError, "尚未完成运行验收"):
            self.finalize("2.0.1")
        self.assertEqual(writes, len(self.github.writes) + len(self.gitee.writes))

    def test_next_version_preserves_old_release_and_uses_historical_maxima(self):
        old = self.finalize()
        self.accept(old)
        old_release = self.github.contents["releases/v2/2.0.0/release.json"]
        self.advance_candidate("2.0.1")
        result = self.finalize("2.0.1")
        self.assertEqual((result["sequence"], result["refreshSequence"]), (2, 2))
        self.assertTrue(result["stateIds"])
        self.assertEqual(
            self.github.contents["releases/v2/2.0.0/release.json"], old_release
        )
        incomplete = self.acceptance(result)
        incomplete["cases"] = [
            case for case in incomplete["cases"] if case["stateId"] == "fresh"
        ]
        with self.assertRaises(ValueError):
            self.accept(result, incomplete)
        self.assertEqual(self.accept(result)["status"], "published")

    def test_deleted_history_is_not_an_uninitialized_channel(self):
        result = self.finalize()
        self.accept(result)
        for channel in (self.github, self.gitee):
            for path in list(channel.contents):
                channel.delete_content(path, "test historical deletion")
        self.advance_candidate("2.0.1")
        report = publication.inspect_publication(
            github=self.github,
            gitee=self.gitee,
            version="2.0.1",
            release_public_key=self.release_public,
            freshness_public_key=self.freshness_public,
        )
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["history"]["maxSequence"], 1)
        self.assertEqual(report["history"]["maxRefreshSequence"], 1)
        writes = len(self.github.writes) + len(self.gitee.writes)
        with self.assertRaisesRegex(RuntimeError, "发行历史"):
            self.finalize("2.0.1")
        self.assertEqual(writes, len(self.github.writes) + len(self.gitee.writes))

    def test_same_version_modified_candidate_is_never_already_active(self):
        self.finalize()
        name = "FQGate-2.0.0-release-request.json"
        request = json.loads(self.github.asset_contents[name])
        request["releaseNotes"] = ["different immutable candidate"]
        content = fixtures.encoded(request)
        self.github.asset_contents[name] = content
        asset = next(
            asset for asset in self.github.release["assets"] if asset["name"] == name
        )
        asset["size"] = len(content)
        asset["digest"] = f"sha256:{protocol.sha256_bytes(content)}"
        writes = len(self.github.writes) + len(self.gitee.writes)
        with self.assertRaisesRegex(RuntimeError, "候选身份已变化"):
            self.finalize()
        self.assertEqual(writes, len(self.github.writes) + len(self.gitee.writes))

    def test_failed_runtime_report_cannot_create_completion(self):
        result = self.finalize()
        report = self.acceptance(result)
        report["cases"][0]["automaticExit"] = True
        with self.assertRaises(ValueError):
            self.accept(result, report)
        self.assertIsNone(
            self.github.read_content(
                publication.publication_path("2.0.0", "completion")
            )
        )

    def test_accept_rechecks_both_public_releases_assets_and_urls_without_writes(self):
        def draft_github():
            self.github.release["draft"] = True

        def hide_gitee():
            self.gitee.release["prerelease"] = True

        def remove_gitee_asset():
            self.gitee.asset_records.pop(next(iter(self.gitee.asset_records)))

        def change_gitee_url():
            name = "FQGate-2.0.0-windows-x64.zip"
            self.gitee.asset_records[name]["browser_download_url"] += "-changed"

        for mutate in (draft_github, hide_gitee, remove_gitee_asset, change_gitee_url):
            with self.subTest(mutation=mutate.__name__):
                self.setUp()
                result = self.finalize()
                mutate()
                writes = len(self.github.writes) + len(self.gitee.writes)
                with self.assertRaises(RuntimeError):
                    self.accept(result)
                self.assertEqual(
                    writes, len(self.github.writes) + len(self.gitee.writes)
                )

    def test_completion_must_bind_the_exact_signed_intent(self):
        result = self.finalize()
        intent_bytes = self.github.contents[
            publication.publication_path("2.0.0", "intent")
        ]
        intent = protocol.verify_document(
            intent_bytes, self.release_public, protocol.RELEASE_SIGNING_KEY_ID
        )
        completion = {
            **{key: intent[key] for key in publication.COMMON_FIELDS},
            "kind": "completion",
            "candidateSha256": result["candidateSha256"],
            "intentSha256": "b" * 64,
            "acceptanceSha256": "a" * 64,
        }
        wrong = protocol.sign_document(
            completion, self.release_key, protocol.RELEASE_SIGNING_KEY_ID
        )
        path = publication.publication_path("2.0.0", "completion")
        for channel in (self.github, self.gitee):
            channel.compare_and_write(
                path, wrong, None, "test deliberately wrong completion"
            )
        self.advance_candidate("2.0.1")
        writes = len(self.github.writes) + len(self.gitee.writes)
        with self.assertRaisesRegex(RuntimeError, "完成记录与在途发布身份不一致"):
            self.finalize("2.0.1")
        self.assertEqual(writes, len(self.github.writes) + len(self.gitee.writes))

    def test_management_references_cannot_be_null(self):
        self.finalize()
        path = publication.publication_path("2.0.0", "intent")
        intent = protocol.verify_document(
            self.github.contents[path],
            self.release_public,
            protocol.RELEASE_SIGNING_KEY_ID,
        )
        intent["reservationSha256"] = None
        with self.assertRaisesRegex(ValueError, "摘要无效"):
            publication.validate_management(path, intent)

    def test_audit_does_not_write_or_publish_and_recognizes_signed_partial(self):
        self.github.fail_write_once.add(protocol.ROOT_FRESHNESS_PATH)
        with self.assertRaises(RuntimeError):
            self.finalize()
        writes = len(self.github.writes) + len(self.gitee.writes)
        report = publication.inspect_publication(
            github=self.github,
            gitee=self.gitee,
            version="2.0.0",
            release_public_key=self.release_public,
            freshness_public_key=self.freshness_public,
        )
        self.assertEqual(report["status"], "resume_existing_reservation")
        self.assertTrue(report["rawAudit"]["violations"])
        self.assertFalse(report["history"]["violations"])
        self.assertEqual(writes, len(self.github.writes) + len(self.gitee.writes))

    def test_stale_history_cannot_allocate_below_new_live_sequence(self):
        self.advance_candidate("2.0.1")
        original = publication.audit_history

        def audit_then_external_push(*args):
            history = original(*args)
            self.inject_external_documents(self.external_chain())
            return history

        with patch.object(
            publication, "audit_history", side_effect=audit_then_external_push
        ):
            with self.assertRaisesRegex(RuntimeError, "必须重新审计"):
                self.finalize("2.0.1")
        active = protocol.read_active_state(
            self.github, self.gitee, self.release_public, self.freshness_public
        )
        self.assertEqual(
            (active["stable"]["sequence"], active["freshness"]["refreshSequence"]),
            (100, 100),
        )
        self.assertIsNone(self.github.read_content(publication.IN_FLIGHT_PATH))
        self.assertTrue(self.github.release["draft"])

    def test_pinned_head_detects_non_v2_change_before_first_write(self):
        original = publication.audit_history

        def audit_then_note(*args):
            history = original(*args)
            self.github.contents["external-note.txt"] = b"changed main"
            self.github.checkpoint()
            return history

        with patch.object(publication, "audit_history", side_effect=audit_then_note):
            with self.assertRaisesRegex(RuntimeError, "必须重新审计"):
                self.finalize()
        self.assertIsNone(self.github.read_content(publication.IN_FLIGHT_PATH))

    def test_switch_reaudits_higher_external_sequence_after_own_deploy(self):
        original = protocol.deploy_immutable
        injected = False

        def deploy_then_external_push(*args):
            nonlocal injected
            original(*args)
            if args[2] == "releases/v2/1/stable.json" and not injected:
                injected = True
                self.inject_external_documents(self.external_chain("2.0.1"))

        with patch.object(
            protocol, "deploy_immutable", side_effect=deploy_then_external_push
        ):
            with self.assertRaisesRegex(RuntimeError, "更高的历史序号"):
                self.finalize()
        active = protocol.read_active_state(
            self.github, self.gitee, self.release_public, self.freshness_public
        )
        self.assertEqual(active["stable"]["sequence"], 100)

    def test_candidate_changed_during_mirror_blocks_before_publication(self):
        original = protocol.mirror_to_gitee

        def mirror_then_change(*args):
            result = original(*args)
            self.replace_candidate_zip_consistently()
            return result

        with patch.object(protocol, "mirror_to_gitee", side_effect=mirror_then_change):
            with self.assertRaisesRegex(RuntimeError, "候选资产已变化"):
                self.finalize()
        self.assertTrue(self.github.release["draft"])
        self.assertTrue(self.gitee.release["prerelease"])
        self.assertIsNone(self.github.read_content(protocol.ROOT_FRESHNESS_PATH))

    def test_candidate_changed_after_publish_cannot_activate_freshness(self):
        original = protocol.publish_release_pair

        def publish_then_change(*args):
            result = original(*args)
            self.replace_candidate_zip_consistently()
            return result

        with patch.object(
            protocol, "publish_release_pair", side_effect=publish_then_change
        ):
            with self.assertRaisesRegex(RuntimeError, "候选资产已变化"):
                self.finalize()
        self.assertIsNone(self.github.read_content(protocol.ROOT_FRESHNESS_PATH))

    def test_removed_reservation_history_anchor_blocks_resume(self):
        self.gitee.fail_write_once.add(publication.IN_FLIGHT_PATH)
        with self.assertRaises(RuntimeError):
            self.finalize()
        self.github.history = [self.github.history[-1]]
        # 不能仅凭仍存在的 reservation 丢掉首次分配时的空通道基线。
        with self.assertRaisesRegex(RuntimeError, "历史锚点不可达"):
            self.finalize()


if __name__ == "__main__":
    unittest.main()
