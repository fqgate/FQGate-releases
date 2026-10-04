import base64
import copy
import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from finalize_v2_release import (
    FRESHNESS_SIGNING_KEY_ID,
    RELEASE_SIGNING_KEY_ID,
    REQUIRED_ENTRIES,
    base64url_encode,
    sign_document,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from v2_history import FRESHNESS_PATH, IN_FLIGHT_PATH, collect_history
from v2_repository_history import assert_history_anchor, history_snapshots


def sha(number):
    return f"{number:040x}"


def key_pair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, base64url_encode(public)


class HistoryChannel:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def history_snapshots(self):
        return self.snapshots[-1][0], copy.deepcopy(self.snapshots)


class SignedFixture:
    def __init__(self):
        self.release_private, self.release_public = key_pair()
        self.fresh_private, self.fresh_public = key_pair()

    def chain(self, version="2.0.0", sequence=1, refresh=1, note="改善使用体验"):
        release_payload = {
            "schemaVersion": 1,
            "channel": "stable",
            "sequence": sequence,
            "version": version,
            "publishedAt": 1_791_020_721,
            "releaseNotes": [note],
            "artifacts": [],
        }
        for platform, arch in (
            ("windows", "x86_64"),
            ("macos", "aarch64"),
            ("macos", "x86_64"),
        ):
            name = f"FQGate-{version}-{platform}-{arch}.zip"
            release_payload["artifacts"].append(
                {
                    "platform": platform,
                    "arch": arch,
                    "fileName": name,
                    "installMode": "replaceApplication",
                    "bytes": 12,
                    "sha256": "a1" * 32,
                    "updatePayload": {
                        "schemaVersion": 1,
                        "format": "zip",
                        "requiredEntries": REQUIRED_ENTRIES[platform],
                    },
                    "downloadUrls": {
                        "github": f"https://github.com/fqgate/FQGate-releases/releases/download/fqgate-v{version}/{name}",
                        "gitee": f"https://gitee.com/qicuo/fqgate-releases/releases/download/fqgate-v{version}/{name}",
                    },
                }
            )
        release_path = f"releases/v2/{version}/release.json"
        release = sign_document(
            release_payload, self.release_private, RELEASE_SIGNING_KEY_ID
        )
        stable_path = f"releases/v2/{sequence}/stable.json"
        stable = sign_document(
            {
                "schemaVersion": 1,
                "channel": "stable",
                "sequence": sequence,
                "version": version,
                "releasePath": f"{version}/release.json",
                "releaseSha256": hashlib.sha256(release).hexdigest(),
            },
            self.release_private,
            RELEASE_SIGNING_KEY_ID,
        )
        fresh = self.freshness(stable, sequence, refresh)
        return {release_path: release, stable_path: stable, FRESHNESS_PATH: fresh}

    def freshness(self, stable, sequence, refresh):
        return sign_document(
            {
                "schemaVersion": 1,
                "channel": "stable",
                "refreshSequence": refresh,
                "stablePath": f"{sequence}/stable.json",
                "stableSha256": hashlib.sha256(stable).hexdigest(),
            },
            self.fresh_private,
            FRESHNESS_SIGNING_KEY_ID,
        )

    def management(self, kind="reservation", version="2.0.1", sequence=5, refresh=8):
        return sign_document(
            {
                "schemaVersion": 1,
                "kind": kind,
                "version": version,
                "sequence": sequence,
                "refreshSequence": refresh,
            },
            self.release_private,
            RELEASE_SIGNING_KEY_ID,
        )

    def collect(self, snapshots, gitee=None, **kwargs):
        return collect_history(
            HistoryChannel(snapshots),
            HistoryChannel(gitee or snapshots),
            self.release_public,
            self.fresh_public,
            **kwargs,
        )


def management_validator(_path, payload):
    if set(payload) != {
        "schemaVersion",
        "kind",
        "version",
        "sequence",
        "refreshSequence",
    }:
        raise ValueError("管理文档字段错误")


class HistoryAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SignedFixture()

    def test_clean_first_release_and_forward_only_transition(self):
        first = self.fixture.chain()
        second = {**first, **self.fixture.chain("2.0.1", 2, 2)}
        audit = self.fixture.collect([(sha(1), first), (sha(2), second)])
        self.assertEqual(audit.max_sequence, 2)
        self.assertEqual(audit.max_refresh_sequence, 2)
        self.assertEqual(audit.heads, {"github": sha(2), "gitee": sha(2)})
        self.assertEqual(len(audit.snapshots), 4)
        self.assertEqual(audit.violations, [])

    def test_current_empty_does_not_erase_deleted_maxima(self):
        old = self.fixture.chain(sequence=9, refresh=12)
        audit = self.fixture.collect([(sha(1), old), (sha(2), {})])
        self.assertEqual((audit.max_sequence, audit.max_refresh_sequence), (9, 12))
        self.assertIn(old[FRESHNESS_PATH], audit.documents[FRESHNESS_PATH])
        self.assertTrue(
            any("删除历史签名文档" in message for message in audit.violations)
        )

    def test_genuinely_empty_complete_history_is_initial(self):
        audit = self.fixture.collect([(sha(1), {})])
        self.assertEqual((audit.max_sequence, audit.max_refresh_sequence), (0, 0))
        self.assertEqual(audit.violations, [])

    def test_forward_freshness_and_in_flight_changes_are_not_immutable(self):
        first = self.fixture.chain()
        first[IN_FLIGHT_PATH] = self.fixture.management()
        second = dict(first)
        second[FRESHNESS_PATH] = self.fixture.freshness(
            first["releases/v2/1/stable.json"], 1, 2
        )
        second[IN_FLIGHT_PATH] = self.fixture.management("reservation", "2.0.2", 6, 9)
        audit = self.fixture.collect(
            [(sha(1), first), (sha(2), second)],
            management_validator=management_validator,
        )
        self.assertEqual((audit.max_sequence, audit.max_refresh_sequence), (6, 9))
        self.assertEqual(audit.violations, [])

    def test_legitimate_legacy_time_fields_remain_original_audit_evidence(self):
        files = self.fixture.chain(sequence=2, refresh=3)
        path = "releases/v2/2.0.0/release.json"
        envelope = json.loads(files[path])
        payload = json.loads(
            base64.urlsafe_b64decode(
                envelope["signed"] + "=" * (-len(envelope["signed"]) % 4)
            )
        )
        payload["expiresAt"] = payload["publishedAt"] + 30 * 86_400
        files[path] = sign_document(
            payload, self.fixture.release_private, RELEASE_SIGNING_KEY_ID
        )
        stable = sign_document(
            {
                "schemaVersion": 1,
                "channel": "stable",
                "sequence": 2,
                "version": "2.0.0",
                "releasePath": "2.0.0/release.json",
                "releaseSha256": hashlib.sha256(files[path]).hexdigest(),
            },
            self.fixture.release_private,
            RELEASE_SIGNING_KEY_ID,
        )
        files["releases/v2/2/stable.json"] = stable
        files[FRESHNESS_PATH] = sign_document(
            {
                "schemaVersion": 1,
                "channel": "stable",
                "refreshSequence": 3,
                "stablePath": "2/stable.json",
                "stableSha256": hashlib.sha256(stable).hexdigest(),
                "issuedAt": payload["publishedAt"],
                "expiresAt": payload["publishedAt"] + 43_200,
            },
            self.fixture.fresh_private,
            FRESHNESS_SIGNING_KEY_ID,
        )
        audit = self.fixture.collect([(sha(1), files)])
        self.assertEqual((audit.max_sequence, audit.max_refresh_sequence), (2, 3))
        self.assertEqual(audit.documents[path], {files[path]})
        self.assertEqual(audit.violations, [])
        for invalid in (True, payload["publishedAt"], 2**64):
            payload["expiresAt"] = invalid
            files[path] = sign_document(
                payload, self.fixture.release_private, RELEASE_SIGNING_KEY_ID
            )
            with self.subTest(expires_at=invalid), self.assertRaisesRegex(
                RuntimeError, "文档验签或合同校验失败"
            ):
                self.fixture.collect([(sha(1), files)])

    def test_same_version_and_sequence_different_bytes_are_evidence(self):
        first = self.fixture.chain()
        second = self.fixture.chain(note="同版本另一份内容")
        audit = self.fixture.collect([(sha(1), first), (sha(2), second)])
        self.assertEqual(len(audit.documents["releases/v2/2.0.0/release.json"]), 2)
        for expected in (
            "不可变历史路径复用",
            "release 历史序号复用",
            "stable 历史序号复用",
            "freshness 历史序号复用",
        ):
            self.assertTrue(
                any(expected in message for message in audit.violations), expected
            )

    def test_deleted_chain_and_reset_are_not_treated_as_first_publish(self):
        old = self.fixture.chain(sequence=2, refresh=3)
        reset = self.fixture.chain(note="正式包重建")
        audit = self.fixture.collect([(sha(1), old), (sha(2), {}), (sha(3), reset)])
        self.assertEqual((audit.max_sequence, audit.max_refresh_sequence), (2, 3))
        self.assertTrue(
            any("Freshness 序号倒退" in message for message in audit.violations)
        )
        self.assertTrue(
            any("当前稳定入口倒退" in message for message in audit.violations)
        )

    def test_historical_broken_links_remain_visible(self):
        first = self.fixture.chain()
        replacement = self.fixture.chain(sequence=2, refresh=2)
        audit = self.fixture.collect(
            [(sha(1), first), (sha(2), {**first, **replacement})]
        )
        self.assertTrue(
            any("历史摘要不匹配" in message for message in audit.violations)
        )

    def test_publication_prefix_is_retained_without_fake_complete_chain(self):
        complete = self.fixture.chain()
        release_path = "releases/v2/2.0.0/release.json"
        audit = self.fixture.collect(
            [(sha(1), {release_path: complete[release_path]}), (sha(2), complete)]
        )
        self.assertEqual(audit.violations, [])

    def test_stable_referencing_missing_release_is_a_violation(self):
        files = self.fixture.chain()
        files.pop("releases/v2/2.0.0/release.json")
        audit = self.fixture.collect([(sha(1), files)])
        self.assertTrue(any("历史断链" in message for message in audit.violations))

    def test_current_mirror_divergence_is_not_silently_repaired(self):
        first = self.fixture.chain()
        second = self.fixture.chain(note="另一源内容")
        audit = self.fixture.collect([(sha(1), first)], [(sha(2), second)])
        self.assertTrue(
            any("双源当前文档字节不一致" in message for message in audit.violations)
        )

    def test_unknown_and_malformed_signed_paths_block(self):
        files = self.fixture.chain()
        for path in ("releases/v2/unknown.json", "releases/v2/3.0.0/release.json"):
            with self.subTest(path=path), self.assertRaisesRegex(
                RuntimeError, "文档验签或合同校验失败"
            ):
                self.fixture.collect(
                    [(sha(1), {path: files["releases/v2/2.0.0/release.json"]})]
                )

    def test_invalid_signature_and_json_block(self):
        files = self.fixture.chain()
        another = SignedFixture().chain()
        for bad in (b"not-json", another[FRESHNESS_PATH]):
            with self.subTest(bad=bad[:12]), self.assertRaisesRegex(
                RuntimeError, "文档验签或合同校验失败"
            ):
                self.fixture.collect([(sha(1), {**files, FRESHNESS_PATH: bad})])

    def test_duplicate_json_field_is_not_accepted_even_with_valid_signature(self):
        files = self.fixture.chain()
        original = json.loads(files[FRESHNESS_PATH])
        signed = base64.urlsafe_b64decode(
            original["signed"] + "=" * (-len(original["signed"]) % 4)
        )
        signed = signed[:-1] + b',"refreshSequence":1}'
        original["signed"] = base64url_encode(signed)
        original["signature"] = base64url_encode(
            self.fixture.fresh_private.sign(signed)
        )
        with self.assertRaisesRegex(RuntimeError, "文档验签或合同校验失败"):
            self.fixture.collect(
                [(sha(1), {**files, FRESHNESS_PATH: json.dumps(original).encode()})]
            )

    def test_missing_head_or_duplicate_snapshot_blocks(self):
        class MissingHead(HistoryChannel):
            def history_snapshots(self):
                return sha(99), self.snapshots

        with self.assertRaisesRegex(RuntimeError, "未覆盖"):
            collect_history(
                MissingHead([(sha(1), {})]),
                HistoryChannel([(sha(1), {})]),
                self.fixture.release_public,
                self.fixture.fresh_public,
            )
        with self.assertRaisesRegex(RuntimeError, "身份重复"):
            self.fixture.collect([(sha(1), {}), (sha(1), {})])

    def test_reservations_and_deleted_lock_also_raise_maxima(self):
        path = "releases/v2/publications/2.0.1/reservation.json"
        reservation = self.fixture.management()
        audit = self.fixture.collect(
            [
                (sha(1), {path: reservation, IN_FLIGHT_PATH: reservation}),
                (sha(2), {path: reservation}),
            ],
            management_validator=management_validator,
        )
        self.assertEqual((audit.max_sequence, audit.max_refresh_sequence), (5, 8))
        self.assertIn(reservation, audit.documents[IN_FLIGHT_PATH])
        self.assertEqual(audit.violations, [])

    def test_intent_and_completion_are_verified_with_callback(self):
        files = {}
        for kind in ("reservation", "intent", "completion"):
            files[f"releases/v2/publications/2.0.1/{kind}.json"] = (
                self.fixture.management(kind)
            )
        audit = self.fixture.collect(
            [(sha(1), files)], management_validator=management_validator
        )
        self.assertEqual((audit.max_sequence, audit.max_refresh_sequence), (5, 8))
        self.assertEqual(audit.violations, [])
        with self.assertRaisesRegex(RuntimeError, "文档验签或合同校验失败"):
            self.fixture.collect([(sha(1), files)])

    def test_reserved_sequence_cannot_be_owned_by_other_version(self):
        files = {
            "releases/v2/publications/2.0.1/reservation.json": self.fixture.management()
        }
        files["releases/v2/publications/2.0.2/intent.json"] = self.fixture.management(
            "intent", "2.0.2"
        )
        audit = self.fixture.collect(
            [(sha(1), files)], management_validator=management_validator
        )
        self.assertTrue(any("预留稳定序号" in message for message in audit.violations))
        self.assertTrue(any("预留刷新序号" in message for message in audit.violations))

    def test_immutable_intent_replacement_is_a_violation(self):
        path = "releases/v2/publications/2.0.1/intent.json"
        old = {path: self.fixture.management("intent")}
        new = {path: self.fixture.management("intent", sequence=6, refresh=9)}
        audit = self.fixture.collect(
            [(sha(1), old), (sha(2), new)], management_validator=management_validator
        )
        self.assertTrue(
            any("不可变历史路径复用" in message for message in audit.violations)
        )

    def test_stable_path_and_linked_release_identity_are_checked(self):
        files = self.fixture.chain()
        wrong_path = dict(files)
        wrong_path["releases/v2/2/stable.json"] = wrong_path.pop(
            "releases/v2/1/stable.json"
        )
        with self.assertRaisesRegex(RuntimeError, "文档验签或合同校验失败"):
            self.fixture.collect([(sha(1), wrong_path)])
        release = self.fixture.chain(sequence=2)["releases/v2/2.0.0/release.json"]
        stable = sign_document(
            {
                "schemaVersion": 1,
                "channel": "stable",
                "sequence": 1,
                "version": "2.0.0",
                "releasePath": "2.0.0/release.json",
                "releaseSha256": hashlib.sha256(release).hexdigest(),
            },
            self.fixture.release_private,
            RELEASE_SIGNING_KEY_ID,
        )
        files.update(
            {
                "releases/v2/2.0.0/release.json": release,
                "releases/v2/1/stable.json": stable,
                FRESHNESS_PATH: self.fixture.freshness(stable, 1, 1),
            }
        )
        audit = self.fixture.collect([(sha(1), files)])
        self.assertTrue(
            any("历史身份不匹配" in message for message in audit.violations)
        )

    def test_bad_reserved_number_and_wrong_management_kind_block(self):
        for body in (
            self.fixture.management(sequence=True),
            self.fixture.management("completion"),
        ):
            with self.assertRaisesRegex(RuntimeError, "文档验签或合同校验失败"):
                self.fixture.collect(
                    [(sha(1), {IN_FLIGHT_PATH: body})],
                    management_validator=management_validator,
                )


class RepositoryApi:
    """严格的只读 Git 对象 fixture；无 Token、账号和真实网络依赖。"""

    repository = "owner/repository"

    def __init__(self, snapshots):
        self.requests = []
        self.blobs = {}
        self.trees = {}
        self.commits = []
        self.main_reads = 0
        self.repeat_page = False
        self.changed_head = False
        self.page_error = False
        self.omit_history = False
        self.allow_unfiltered_history = False
        for number, files in snapshots:
            entries = []
            for path, content in files.items():
                blob_sha = hashlib.sha1(
                    f"blob {len(content)}\0".encode() + content
                ).hexdigest()
                self.blobs[blob_sha] = {
                    "sha": blob_sha,
                    "size": len(content),
                    "encoding": "base64",
                    "content": base64.encodebytes(content).decode(),
                }
                entries.append(
                    {
                        "path": path,
                        "sha": blob_sha,
                        "size": len(content),
                        "type": "blob",
                        "mode": "100644",
                    }
                )
            tree_sha = sha(number + 10_000)
            self.trees[tree_sha] = {
                "sha": tree_sha,
                "truncated": False,
                "tree": entries,
            }
            self.commits.append(
                {"sha": sha(number), "commit": {"tree": {"sha": tree_sha}}}
            )
        self.head = self.commits[-1]

    def json_request(self, method, path):
        if method != "GET":
            raise AssertionError("历史审计禁止写入 API")
        self.requests.append(path)
        url = urlsplit(path)
        if url.path.endswith("/commits/main"):
            self.main_reads += 1
            result = copy.deepcopy(self.head)
            if self.changed_head and self.main_reads > 1:
                result["sha"] = sha(999_999)
            return result
        if url.path.endswith("/commits"):
            query = parse_qs(url.query)
            assert query["sha"] == [self.head["sha"]]
            if self.allow_unfiltered_history:
                assert "path" not in query
            else:
                assert query["path"] == ["releases/v2"]
            assert query["per_page"] == ["100"]
            if self.page_error:
                raise RuntimeError("远端分页失败")
            if self.omit_history:
                return []
            page = 1 if self.repeat_page else int(query["page"][0])
            start = (page - 1) * 100
            return copy.deepcopy(list(reversed(self.commits))[start : start + 100])
        if "/git/trees/" in url.path:
            assert parse_qs(url.query) == {"recursive": ["1"]}
            return copy.deepcopy(self.trees[url.path.rsplit("/", 1)[1]])
        if "/git/blobs/" in url.path:
            return copy.deepcopy(self.blobs[url.path.rsplit("/", 1)[1]])
        raise AssertionError(path)


class RepositoryHistoryTests(unittest.TestCase):
    def test_both_channels_read_all_pages_and_cache_blob_bytes(self):
        for channel in ("github", "gitee"):
            with self.subTest(channel=channel):
                api = RepositoryApi(
                    [
                        (number, {FRESHNESS_PATH: b"same bytes"})
                        for number in range(1, 122)
                    ]
                )
                head, snapshots = history_snapshots(api, channel)
                self.assertEqual(head, sha(121))
                self.assertEqual(len(snapshots), 121)
                self.assertEqual(snapshots[0][0], sha(1))
                pages = [path for path in api.requests if "/commits?" in path]
                self.assertEqual(len(pages), 3)
                self.assertEqual(sum("/git/blobs/" in path for path in api.requests), 1)
                self.assertEqual(api.main_reads, 2)

    def test_short_page_is_followed_by_explicit_empty_page(self):
        api = RepositoryApi([(1, {})])
        history_snapshots(api, "github")
        self.assertEqual(sum("/commits?" in path for path in api.requests), 2)

    def test_pinned_head_is_included_when_only_other_paths_changed(self):
        api = RepositoryApi(
            [(1, {FRESHNESS_PATH: b"one"}), (2, {FRESHNESS_PATH: b"one"})]
        )
        api.commits.pop()
        head, snapshots = history_snapshots(api, "gitee")
        self.assertEqual(head, sha(2))
        self.assertEqual([item[0] for item in snapshots], [sha(1), sha(2)])

    def test_empty_history_requires_current_tree_to_be_empty(self):
        api = RepositoryApi([(1, {})])
        api.omit_history = True
        self.assertEqual(history_snapshots(api, "github"), (sha(1), [(sha(1), {})]))
        api = RepositoryApi([(1, {FRESHNESS_PATH: b"not empty"})])
        api.omit_history = True
        with self.assertRaisesRegex(RuntimeError, "遗漏"):
            history_snapshots(api, "github")

    def test_omitted_head_change_is_not_accepted(self):
        api = RepositoryApi(
            [(1, {FRESHNESS_PATH: b"one"}), (2, {FRESHNESS_PATH: b"two"})]
        )
        api.commits.pop()
        with self.assertRaisesRegex(RuntimeError, "遗漏"):
            history_snapshots(api, "github")

    def test_truncated_or_malformed_tree_blocks(self):
        for value in (True, None):
            api = RepositoryApi([(1, {})])
            api.trees[sha(10_001)]["truncated"] = value
            with self.assertRaisesRegex(RuntimeError, "截断"):
                history_snapshots(api, "github")

    def test_repeated_pagination_and_api_error_block(self):
        api = RepositoryApi([(1, {})])
        api.repeat_page = True
        with self.assertRaisesRegex(RuntimeError, "分页重复"):
            history_snapshots(api, "gitee")
        api = RepositoryApi([(1, {})])
        api.page_error = True
        with self.assertRaisesRegex(RuntimeError, "分页失败"):
            history_snapshots(api, "github")

    def test_blob_corruption_encoding_and_size_block(self):
        for field, value in (
            ("content", base64.b64encode(b"wrong").decode()),
            ("encoding", "utf-8"),
            ("size", 999),
        ):
            api = RepositoryApi([(1, {FRESHNESS_PATH: b"original"})])
            next(iter(api.blobs.values()))[field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                history_snapshots(api, "github")

    def test_symlink_duplicate_and_traversal_paths_block(self):
        for mutation in ("symlink", "duplicate", "traversal"):
            api = RepositoryApi([(1, {FRESHNESS_PATH: b"original"})])
            entries = api.trees[sha(10_001)]["tree"]
            if mutation == "symlink":
                entries[0]["mode"] = "120000"
            elif mutation == "duplicate":
                entries.append(copy.deepcopy(entries[0]))
            else:
                entries[0]["path"] = "releases/v2/../freshness.json"
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                history_snapshots(api, "gitee")

    def test_main_change_during_audit_blocks(self):
        api = RepositoryApi([(1, {})])
        api.changed_head = True
        with self.assertRaisesRegex(RuntimeError, "main.*发生变化"):
            history_snapshots(api, "github")

    def test_bad_commit_and_wrong_tree_identity_block(self):
        api = RepositoryApi([(1, {})])
        api.head["commit"]["tree"]["sha"] = "short"
        with self.assertRaisesRegex(RuntimeError, "Git SHA"):
            history_snapshots(api, "github")
        api = RepositoryApi([(1, {})])
        api.trees[sha(10_001)]["sha"] = sha(22_222)
        with self.assertRaisesRegex(RuntimeError, "树身份错误"):
            history_snapshots(api, "gitee")


class HistoryAnchorTests(unittest.TestCase):
    def api(self, count=1):
        api = RepositoryApi([(number, {}) for number in range(1, count + 1)])
        api.allow_unfiltered_history = True
        return api

    def test_non_v2_anchor_is_found_through_all_history_pagination(self):
        for channel in ("github", "gitee"):
            with self.subTest(channel=channel):
                api = self.api(121)
                self.assertIsNone(assert_history_anchor(api, channel, sha(1), sha(121)))
                pages = [path for path in api.requests if "/commits?" in path]
                self.assertEqual(len(pages), 2)
                self.assertTrue(all("path=" not in path for path in pages))
                self.assertEqual(api.main_reads, 2)
                self.assertFalse(any("/git/" in path for path in api.requests))

    def test_unreachable_anchor_is_not_proven_by_an_existing_old_object(self):
        api = self.api(2)
        old = {"sha": sha(99), "commit": {"tree": {"sha": sha(10_099)}}}
        original_request = api.json_request

        def request(method, path):
            if path.endswith(f"/commits/{sha(99)}"):
                return old
            return original_request(method, path)

        api.json_request = request
        with self.assertRaisesRegex(RuntimeError, "基线不再.*可达"):
            assert_history_anchor(api, "github", sha(99), sha(2))
        self.assertEqual(sum("/commits?" in path for path in api.requests), 2)

    def test_repeated_history_page_blocks_anchor_search(self):
        api = self.api(121)
        api.repeat_page = True
        with self.assertRaisesRegex(RuntimeError, "全历史分页重复"):
            assert_history_anchor(api, "gitee", sha(1), sha(121))

    def test_main_drift_after_anchor_found_blocks(self):
        api = self.api()
        api.changed_head = True
        with self.assertRaisesRegex(RuntimeError, "main.*发生变化"):
            assert_history_anchor(api, "github", sha(1), sha(1))

    def test_pinned_head_must_match_main_before_search(self):
        api = self.api(2)
        with self.assertRaisesRegex(RuntimeError, "钉住的 SHA 不一致"):
            assert_history_anchor(api, "github", sha(1), sha(3))
        self.assertFalse(any("/commits?" in path for path in api.requests))

    def test_malformed_commit_and_api_failure_block(self):
        api = self.api(2)
        api.commits[0]["commit"]["tree"]["sha"] = "short"
        with self.assertRaisesRegex(RuntimeError, "Git SHA"):
            assert_history_anchor(api, "gitee", sha(1), sha(2))
        api = self.api()
        api.page_error = True
        with self.assertRaisesRegex(RuntimeError, "分页失败"):
            assert_history_anchor(api, "github", sha(1), sha(1))

    def test_page_not_starting_at_pinned_head_is_not_reachability_proof(self):
        api = self.api(3)
        api.commits.pop()
        with self.assertRaisesRegex(RuntimeError, "钉住的 SHA 不一致"):
            assert_history_anchor(api, "gitee", sha(1), sha(3))


if __name__ == "__main__":
    unittest.main()
