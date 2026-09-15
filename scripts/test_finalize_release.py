import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from finalize_release import build_candidate, promote_manifest, validate_platform_metadata


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


class FakeApi:
    def __init__(self, release, contents):
        self.release = release
        self.contents = contents

    def list_releases(self, _repository):
        return [self.release]

    def download_asset(self, asset):
        return self.contents[asset["name"]]


class FinalizeReleaseTests(unittest.TestCase):
    def create_release(self, version="1.2.3"):
        contents = {}
        metadata_names = []
        definitions = [
            ("windows", "x86_64", "replaceExecutable", f"FQGate-{version}-windows-x64-UNSIGNED.exe", [f"FQGate-{version}-windows-x64-UNSIGNED.zip"]),
            ("macos", "aarch64", "openPackage", f"FQGate-{version}-macos-arm64-ADHOC.zip", []),
            ("macos", "x86_64", "openPackage", f"FQGate-{version}-macos-x86_64-ADHOC.zip", []),
        ]
        for platform, architecture, install_mode, package_name, extras in definitions:
            asset_names = [package_name, *extras]
            descriptions = []
            for name in asset_names:
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
                            "sha256": hashlib.sha256(contents[checksum_name]).hexdigest(),
                        },
                    ]
                )
            package_description = next(item for item in descriptions if item["fileName"] == package_name)
            metadata = {
                "schemaVersion": 1,
                "component": "fqgate",
                "version": version,
                "tag": f"fqgate-v{version}",
                "package": {
                    "platform": platform,
                    "architecture": architecture,
                    "installMode": install_mode,
                    **package_description,
                },
                "assets": descriptions,
            }
            metadata_name = f"{Path(package_name).stem}.release.json"
            metadata_names.append(metadata_name)
            contents[metadata_name] = encoded(metadata)
        request_name = f"FQGate-{version}-release-request.json"
        contents[request_name] = encoded(
            {
                "schemaVersion": 1,
                "component": "fqgate-release-request",
                "version": version,
                "tag": f"fqgate-v{version}",
                "source": {"repository": "zhuyifang/fqgate", "commit": "a" * 40},
                "minimumSupportedVersion": "1.0.0",
                "releaseNotes": ["修复问题"],
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
        return FakeApi(release, contents)

    def test_candidate_validates_all_release_assets(self):
        candidate = build_candidate(self.create_release(), "zhuyifang/fqgate-releases", "1.2.3")
        self.assertEqual(candidate["manifest"]["status"], "unpublished")
        self.assertEqual(len(candidate["manifest"]["packages"]), 3)

    def test_candidate_rejects_an_extra_asset(self):
        api = self.create_release()
        api.release["assets"].append({"name": "extra.txt", "size": 1, "url": "unused"})
        with self.assertRaisesRegex(ValueError, "多余"):
            build_candidate(api, "zhuyifang/fqgate-releases", "1.2.3")

    def test_accepts_current_build_id_metadata(self):
        api = self.create_release()
        for name, content in api.contents.items():
            if not name.endswith(".release.json"):
                continue
            metadata = json.loads(content)
            metadata["buildId"] = "20260915T000000Z"
            validate_platform_metadata(metadata, "1.2.3")
            metadata["buildId"] = "invalid/build"
            with self.assertRaisesRegex(ValueError, "构建编号无效"):
                validate_platform_metadata(metadata, "1.2.3")

    def test_promote_updates_stable_manifest_and_readme(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "releases").mkdir()
            (root / "releases" / "stable.json").write_text('{"version":"1.2.2"}', encoding="utf-8")
            (root / "README.md").write_text(
                "标题\n\n## 下载 FQGate\n\n旧内容\n\n## 使用方法\n\n正文\n", encoding="utf-8"
            )
            manifest = build_candidate(self.create_release(), "zhuyifang/fqgate-releases", "1.2.3")["manifest"]
            manifest.update({"status": "published", "publishedAt": "2026-01-02T03:04:05Z"})
            promote_manifest(manifest, root)
            stable = json.loads((root / "releases" / "stable.json").read_text(encoding="utf-8"))
            self.assertEqual(stable["version"], "1.2.3")
            self.assertIn("FQGate v1.2.3", (root / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
