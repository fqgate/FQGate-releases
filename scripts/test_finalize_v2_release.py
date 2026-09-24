import hashlib
import io
import json
import unittest
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from finalize_v2_release import (
    CURRENT_SOURCE_REPOSITORY,
    DEFAULT_GITHUB_REPOSITORY,
    FRESHNESS_LIFETIME_SECONDS,
    FRESHNESS_SIGNING_KEY_ID,
    RELEASE_LIFETIME_SECONDS,
    RELEASE_SIGNING_KEY_ID,
    ROOT_FRESHNESS_PATH,
    V2_RELEASE_ROOT,
    base64url_encode,
    build_candidate,
    build_release_payload,
    deploy_immutable,
    finalize_release,
    load_signing_key,
    read_active_state,
    refresh_freshness,
    sign_document,
    switch_freshness,
    validate_platform_metadata,
    validate_v2_version,
    validate_update_bundle_bytes,
    verify_document,
)


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


def key_pair():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64url_encode(public)


class ContentChannel:
    def __init__(self):
        self.contents = {}
        self.fail_write_once = set()
        self.writes = []

    def read_content(self, path):
        return self.contents.get(path)

    def write_content(self, path, content, message):
        if path in self.fail_write_once:
            self.fail_write_once.remove(path)
            raise RuntimeError(f"write failed: {path}")
        self.contents[path] = content
        self.writes.append((path, message))

    def delete_content(self, path, _message):
        self.contents.pop(path, None)


class FakeGitHub(ContentChannel):
    def __init__(self, release, asset_contents, events=None):
        super().__init__()
        self.repository = DEFAULT_GITHUB_REPOSITORY
        self.release = release
        self.asset_contents = asset_contents
        self.events = events if events is not None else []
        self.fail_publish = False

    def list_releases(self, _repository=None):
        return [self.release]

    def release_by_tag(self, tag):
        return self.release if self.release["tag_name"] == tag else None

    def assets(self, _release):
        return self.release["assets"]

    def download_asset(self, asset):
        return self.asset_contents[asset["name"]]

    def publish_release(self, _release, _body):
        self.events.append("github-publish")
        if self.fail_publish:
            raise RuntimeError("github publish failed")
        self.release["draft"] = False
        self.release["published_at"] = "2026-09-23T01:02:03Z"
        return self.release


class FakeGitee(ContentChannel):
    def __init__(self, events=None):
        super().__init__()
        self.events = events if events is not None else []
        self.release = None
        self.asset_contents = {}
        self.asset_records = {}
        self.next_asset_id = 1
        self.fail_publish = False
        self.corrupt_upload = False

    def release_by_tag(self, tag):
        if self.release and self.release["tag_name"] == tag:
            return self.release
        return None

    def get_or_create_release(self, tag, body):
        if self.release is None:
            self.release = {
                "id": 20,
                "tag_name": tag,
                "target_commitish": "main",
                "name": tag,
                "body": body,
                "prerelease": True,
            }
        return self.release

    def assets(self, _release):
        return list(self.asset_records.values())

    def download_asset(self, _release, asset):
        return self.asset_contents[asset_name(asset)]

    def upload_asset(self, _release, name, content):
        asset = {
            "id": self.next_asset_id,
            "name": name,
            "browser_download_url": f"https://gitee.com/qicuo/fqgate-releases/attach_files/{self.next_asset_id}/download/{name}",
        }
        self.next_asset_id += 1
        self.asset_records[name] = asset
        self.asset_contents[name] = (
            content + b"corrupt" if self.corrupt_upload else content
        )
        return asset

    def delete_asset(self, _release, asset):
        name = asset_name(asset)
        self.asset_records.pop(name, None)
        self.asset_contents.pop(name, None)

    def set_prerelease(self, release, prerelease):
        self.events.append("gitee-prerelease" if prerelease else "gitee-publish")
        if not prerelease and self.fail_publish:
            raise RuntimeError("gitee publish failed")
        release["prerelease"] = prerelease
        return release


def asset_name(asset):
    return asset.get("name", asset.get("filename"))


class FinalizeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.release_private, self.release_public = key_pair()
        self.freshness_private, self.freshness_public = key_pair()

    def create_github(
        self,
        version="2.0.0",
        source_repository=CURRENT_SOURCE_REPOSITORY,
        events=None,
    ):
        contents = {}
        definitions = [
            (
                "windows",
                "x86_64",
                "replaceApplication",
                f"FQGate-{version}-windows-x64.zip",
                [f"FQGate-{version}-windows-x64.exe"],
            ),
            (
                "macos",
                "aarch64",
                "replaceApplication",
                f"FQGate-{version}-macos-arm64.zip",
                [],
            ),
            (
                "macos",
                "x86_64",
                "replaceApplication",
                f"FQGate-{version}-macos-x86_64.zip",
                [],
            ),
        ]
        for platform, architecture, install_mode, package_name, extras in definitions:
            asset_names = [package_name, *extras]
            descriptions = []
            for name in asset_names:
                if name == package_name:
                    archive_bytes = io.BytesIO()
                    required_entries = (
                        ["FQGate.exe", "fqgate-updater.exe"]
                        if platform == "windows"
                        else [
                            "FQGate.app/Contents/MacOS/fqgate",
                            "FQGate.app/Contents/MacOS/fqgate-updater",
                        ]
                    )
                    with zipfile.ZipFile(archive_bytes, "w") as archive:
                        for entry in required_entries:
                            archive.writestr(entry, f"payload:{entry}".encode())
                    content = archive_bytes.getvalue()
                else:
                    content = f"payload:{name}".encode()
                digest = hashlib.sha256(content).hexdigest()
                contents[name] = content
                checksum_name = f"{name}.sha256"
                contents[checksum_name] = f"{digest}  {name}\n".encode("ascii")
                descriptions.extend(
                    [
                        {"fileName": name, "size": len(content), "sha256": digest},
                        {
                            "fileName": checksum_name,
                            "size": len(contents[checksum_name]),
                            "sha256": hashlib.sha256(
                                contents[checksum_name]
                            ).hexdigest(),
                        },
                    ]
                )
            package = next(
                item for item in descriptions if item["fileName"] == package_name
            )
            required_entries = (
                ["FQGate.exe", "fqgate-updater.exe"]
                if platform == "windows"
                else [
                    "FQGate.app/Contents/MacOS/fqgate",
                    "FQGate.app/Contents/MacOS/fqgate-updater",
                ]
            )
            metadata = {
                "schemaVersion": 1,
                "component": "fqgate",
                "version": version,
                "buildId": "20260923T010203Z",
                "tag": f"fqgate-v{version}",
                "package": {
                    "platform": platform,
                    "architecture": architecture,
                    "installMode": install_mode,
                    **package,
                },
                "updatePayload": {
                    "schemaVersion": 1,
                    "format": "zip",
                    "requiredEntries": required_entries,
                },
                "assets": descriptions,
            }
            metadata_name = f"{Path(package_name).stem}.release.json"
            contents[metadata_name] = encoded(metadata)
        request_name = f"FQGate-{version}-release-request.json"
        contents[request_name] = encoded(
            {
                "schemaVersion": 1,
                "component": "fqgate-release-request",
                "version": version,
                "tag": f"fqgate-v{version}",
                "source": {"repository": source_repository, "commit": "a" * 40},
                "minimumSupportedVersion": "1.0.0",
                "releaseNotes": ["完成 2.0 首次发布"],
            }
        )
        assets = []
        for index, (name, content) in enumerate(contents.items(), 1):
            assets.append(
                {
                    "id": index,
                    "name": name,
                    "size": len(content),
                    "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
                    "url": f"https://api.github.test/assets/{index}",
                }
            )
        release = {
            "id": 10,
            "tag_name": f"fqgate-v{version}",
            "draft": True,
            "prerelease": False,
            "published_at": None,
            "assets": assets,
        }
        return FakeGitHub(release, contents, events)

    def test_candidate_validates_update_payload_and_zip_packages(self):
        candidate = build_candidate(
            self.create_github(), DEFAULT_GITHUB_REPOSITORY, "2.0.0"
        )
        self.assertTrue(candidate["draft"])
        self.assertEqual(len(candidate["packages"]), 3)
        self.assertTrue(
            all(
                item["package"]["fileName"].endswith(".zip")
                for item in candidate["packages"]
            )
        )
        self.assertEqual(
            candidate["packages"][0]["requiredEntries"],
            ["FQGate.exe", "fqgate-updater.exe"],
        )

    def test_platform_metadata_rejects_mismatched_update_payload(self):
        github = self.create_github()
        metadata_name = next(
            name for name in github.asset_contents if name.endswith(".release.json")
        )
        metadata = json.loads(github.asset_contents[metadata_name])
        metadata["updatePayload"]["format"] = "fqgate-update-bundle-v1"
        with self.assertRaisesRegex(ValueError, "updatePayload"):
            validate_platform_metadata(metadata, "2.0.0")

    def test_consumes_current_main_metadata_contract_verbatim(self):
        github = self.create_github()
        metadata_name = next(
            name
            for name in github.asset_contents
            if name.endswith("windows-x64.release.json")
        )
        metadata = json.loads(github.asset_contents[metadata_name])
        target, package, update_payload, _ = validate_platform_metadata(
            metadata, "2.0.0"
        )
        self.assertEqual(target, ("windows", "x86_64", "replaceApplication"))
        self.assertEqual(package["installMode"], "replaceApplication")
        self.assertEqual(
            update_payload,
            {
                "schemaVersion": 1,
                "format": "zip",
                "requiredEntries": ["FQGate.exe", "fqgate-updater.exe"],
            },
        )

    def test_update_zip_must_contain_nonempty_program_and_updater(self):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("FQGate.exe", b"program")
        with self.assertRaisesRegex(ValueError, "缺少必需"):
            validate_update_bundle_bytes(
                archive_bytes.getvalue(), "windows", "FQGate-2.0.0-windows-x64.zip"
            )

    def test_separate_key_types_are_enforced(self):
        release_document = sign_document(
            {"kind": "release"}, self.release_private, RELEASE_SIGNING_KEY_ID
        )
        self.assertEqual(
            verify_document(
                release_document, self.release_public, RELEASE_SIGNING_KEY_ID
            ),
            {"kind": "release"},
        )
        with self.assertRaisesRegex(ValueError, "错误类型"):
            verify_document(
                release_document, self.release_public, FRESHNESS_SIGNING_KEY_ID
            )
        with self.assertRaisesRegex(ValueError, "验签失败"):
            verify_document(
                release_document, self.freshness_public, RELEASE_SIGNING_KEY_ID
            )
        release_seed = self.release_private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with self.assertRaisesRegex(ValueError, "公钥不匹配"):
            load_signing_key(base64url_encode(release_seed), self.freshness_public)

    def test_release_payload_uses_actual_publish_time_and_dual_urls(self):
        candidate = build_candidate(
            self.create_github(), DEFAULT_GITHUB_REPOSITORY, "2.0.0"
        )
        mirrored = {
            item["package"]["fileName"]: {
                "name": item["package"]["fileName"],
                "browser_download_url": f"https://gitee.com/qicuo/fqgate-releases/attach_files/1/download/{item['package']['fileName']}",
            }
            for item in candidate["packages"]
        }
        payload = build_release_payload(candidate, mirrored, 7, 1_700_000_000)
        self.assertEqual(payload["expiresAt"], 1_700_000_000 + RELEASE_LIFETIME_SECONDS)
        self.assertEqual(payload["sequence"], 7)
        self.assertEqual(len(payload["artifacts"]), 3)
        for artifact in payload["artifacts"]:
            self.assertEqual(artifact["installMode"], "replaceApplication")
            self.assertEqual(artifact["updatePayload"]["format"], "zip")
            self.assertEqual(set(artifact["downloadUrls"]), {"github", "gitee"})

    def test_v2_publisher_rejects_1_x_versions(self):
        with self.assertRaisesRegex(ValueError, "只接受 2.0"):
            validate_v2_version("1.0.3")

    def test_github_publish_failure_restores_gitee_prerelease(self):
        events = []
        github = self.create_github(events=events)
        github.fail_publish = True
        gitee = FakeGitee(events)
        candidate = build_candidate(github, github.repository, "2.0.0")
        gitee_release = gitee.get_or_create_release(candidate["tag"], "body")
        from finalize_v2_release import publish_release_pair

        with self.assertRaisesRegex(RuntimeError, "github publish failed"):
            publish_release_pair(github, gitee, candidate, gitee_release)
        self.assertTrue(gitee.release["prerelease"])
        self.assertEqual(
            events, ["gitee-publish", "github-publish", "gitee-prerelease"]
        )

    def test_gitee_publish_is_a_hard_gate(self):
        events = []
        github = self.create_github(events=events)
        gitee = FakeGitee(events)
        gitee.fail_publish = True
        candidate = build_candidate(github, github.repository, "2.0.0")
        gitee_release = gitee.get_or_create_release(candidate["tag"], "body")
        from finalize_v2_release import publish_release_pair

        with self.assertRaisesRegex(RuntimeError, "gitee publish failed"):
            publish_release_pair(github, gitee, candidate, gitee_release)
        self.assertTrue(github.release["draft"])
        self.assertEqual(events, ["gitee-publish"])

    def test_mirror_readback_fails_before_either_release_is_public(self):
        events = []
        github = self.create_github(events=events)
        gitee = FakeGitee(events)
        gitee.corrupt_upload = True
        with self.assertRaisesRegex(RuntimeError, "回读不一致"):
            finalize_release(
                github=github,
                gitee=gitee,
                version="2.0.0",
                release_private_key=self.release_private,
                freshness_private_key=self.freshness_private,
                release_public_key=self.release_public,
                freshness_public_key=self.freshness_public,
                now=1_800_000_000,
            )
        self.assertTrue(github.release["draft"])
        self.assertTrue(gitee.release["prerelease"])
        self.assertEqual(events, [])

    def test_freshness_switch_rolls_back_both_sources(self):
        github = ContentChannel()
        gitee = ContentChannel()
        previous = b"old"
        github.contents[ROOT_FRESHNESS_PATH] = previous
        gitee.contents[ROOT_FRESHNESS_PATH] = previous
        github.fail_write_once.add(ROOT_FRESHNESS_PATH)
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            switch_freshness(github, gitee, b"new", previous, "test")
        self.assertEqual(github.read_content(ROOT_FRESHNESS_PATH), previous)
        self.assertEqual(gitee.read_content(ROOT_FRESHNESS_PATH), previous)

    def test_immutable_deploy_rolls_back_first_source(self):
        github = ContentChannel()
        gitee = ContentChannel()
        path = f"{V2_RELEASE_ROOT}/2.0.0/release.json"
        github.fail_write_once.add(path)
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            deploy_immutable(github, gitee, path, b"signed", "test")
        self.assertIsNone(github.read_content(path))
        self.assertIsNone(gitee.read_content(path))

    def test_finalize_and_refresh_preserve_immutable_documents(self):
        events = []
        github = self.create_github(events=events)
        gitee = FakeGitee(events)
        result = finalize_release(
            github=github,
            gitee=gitee,
            version="2.0.0",
            release_private_key=self.release_private,
            freshness_private_key=self.freshness_private,
            release_public_key=self.release_public,
            freshness_public_key=self.freshness_public,
            now=1_800_000_000,
        )
        self.assertEqual(result["status"], "published")
        self.assertEqual(events[-2:], ["gitee-publish", "github-publish"])
        self.assertEqual(github.contents, gitee.contents)
        self.assertNotIn("releases/stable.json", github.contents)
        self.assertTrue(
            all(path.startswith(f"{V2_RELEASE_ROOT}/") for path in github.contents)
        )
        active = read_active_state(
            github, gitee, self.release_public, self.freshness_public
        )
        self.assertEqual(active["stable"]["sequence"], 1)
        self.assertEqual(active["freshness"]["stablePath"], "1/stable.json")
        self.assertEqual(active["release"]["publishedAt"], 1_790_125_323)
        self.assertEqual(
            active["release"]["expiresAt"], 1_790_125_323 + RELEASE_LIFETIME_SECONDS
        )
        release_before = active["releaseBytes"]
        stable_before = active["stableBytes"]
        refresh = refresh_freshness(
            github=github,
            gitee=gitee,
            freshness_private_key=self.freshness_private,
            release_public_key=self.release_public,
            freshness_public_key=self.freshness_public,
            now=1_800_014_400,
        )
        self.assertEqual(refresh["refreshSequence"], 2)
        active_after = read_active_state(
            github, gitee, self.release_public, self.freshness_public
        )
        self.assertEqual(active_after["releaseBytes"], release_before)
        self.assertEqual(active_after["stableBytes"], stable_before)
        self.assertEqual(
            active_after["freshness"]["expiresAt"],
            1_800_014_400 + FRESHNESS_LIFETIME_SECONDS,
        )

    def test_cross_source_entry_mismatch_is_rejected(self):
        github = ContentChannel()
        gitee = ContentChannel()
        github.contents[ROOT_FRESHNESS_PATH] = b"one"
        gitee.contents[ROOT_FRESHNESS_PATH] = b"two"
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            read_active_state(github, gitee, self.release_public, self.freshness_public)

    def test_workflow_separates_secrets_and_schedules_four_hour_refresh(self):
        workflow = (
            Path(__file__).parents[1] / ".github/workflows/finalize-v2-release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("types: [fqgate-v2-draft-ready]", workflow)
        self.assertIn("scripts/finalize_v2_release.py", workflow)
        self.assertIn("cron: '17 */4 * * *'", workflow)
        self.assertIn("FQGATE_UPDATE_RELEASE_SIGNING_V1_PRIVATE_KEY", workflow)
        self.assertIn("FQGATE_UPDATE_FRESHNESS_SIGNING_V1_PRIVATE_KEY", workflow)
        refresh_job = workflow.split("refresh-freshness:", 1)[1]
        self.assertNotIn("FQGATE_UPDATE_RELEASE_SIGNING_V1_PRIVATE_KEY", refresh_job)
        self.assertIn("FQGATE_UPDATE_FRESHNESS_SIGNING_V1_PRIVATE_KEY", refresh_job)


if __name__ == "__main__":
    unittest.main()
