"""校验 FQGate 草稿资产，正式发布并维护稳定通道。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
EXPECTED_TARGETS = {
    ("windows", "x86_64", "replaceExecutable"),
    ("macos", "aarch64", "openPackage"),
    ("macos", "x86_64", "openPackage"),
}
FORBIDDEN_NAME_PATTERN = re.compile(r"(?:TEST|DEBUG)", re.IGNORECASE)


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"版本号必须是严格语义版本：{value}")
    return tuple(int(part) for part in match.groups())


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class GitHubApi:
    def __init__(self, token: str, api_root="https://api.github.com"):
        self.token = token
        self.api_root = api_root.rstrip("/")

    def request(self, method, path, value=None, accept="application/vnd.github+json"):
        url = path if path.startswith("https://") else self.api_root + path
        body = None if value is None else json.dumps(value).encode("utf-8")
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "fqgate-release-finalizer",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {url} 返回 {error.code}：{detail}") from error
        if "json" in content_type:
            return json.loads(payload) if payload else None
        return payload

    def list_releases(self, repository: str):
        return self.request("GET", f"/repos/{repository}/releases?per_page=100")

    def download_asset(self, asset: dict) -> bytes:
        return self.request("GET", asset["url"], accept="application/octet-stream")

    def publish_release(self, repository: str, release_id: int, body: str):
        return self.request(
            "PATCH",
            f"/repos/{repository}/releases/{release_id}",
            {"body": body, "draft": False, "prerelease": False, "make_latest": "true"},
        )


def read_json_bytes(value: bytes, label: str) -> dict:
    try:
        result = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} 不是有效的 UTF-8 JSON：{error}") from error
    if not isinstance(result, dict):
        raise ValueError(f"{label} 根节点必须是对象")
    return result


def find_release(api, repository: str, tag: str) -> dict:
    matches = [release for release in api.list_releases(repository) if release.get("tag_name") == tag]
    if len(matches) != 1:
        raise ValueError(f"必须存在且只能存在一个 {tag} Release")
    release = matches[0]
    if release.get("prerelease"):
        raise ValueError("稳定通道不能使用预发行 Release")
    return release


def validate_request(request: dict, version: str) -> None:
    if set(request) != {
        "schemaVersion",
        "component",
        "version",
        "tag",
        "source",
        "minimumSupportedVersion",
        "releaseNotes",
    }:
        raise ValueError("发行请求字段不符合约定")
    if request["schemaVersion"] != 1 or request["component"] != "fqgate-release-request":
        raise ValueError("发行请求组件不正确")
    if request["version"] != version or request["tag"] != f"fqgate-v{version}":
        raise ValueError("发行请求版本不一致")
    source = request.get("source")
    if not isinstance(source, dict) or set(source) != {"repository", "commit"}:
        raise ValueError("发行请求缺少源码身份")
    if source["repository"] != "zhuyifang/fqgate" or not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise ValueError("发行请求源码身份无效")
    minimum = str(request["minimumSupportedVersion"])
    if parse_version(minimum) > parse_version(version):
        raise ValueError("最低支持版本不能高于发行版本")
    notes = request.get("releaseNotes")
    if not isinstance(notes, list) or not 1 <= len(notes) <= 8:
        raise ValueError("发行请求必须包含 1 到 8 条更新说明")
    if any(not isinstance(note, str) or not note.strip() or len(note) > 200 for note in notes):
        raise ValueError("发行请求包含无效的更新说明")


def validate_platform_metadata(metadata: dict, version: str) -> tuple[tuple[str, str, str], dict, list[dict]]:
    if set(metadata) - {"buildId"} != {"schemaVersion", "component", "version", "tag", "package", "assets"}:
        raise ValueError("平台元数据字段不符合约定")
    if "buildId" in metadata and (
        not isinstance(metadata["buildId"], str)
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", metadata["buildId"])
    ):
        raise ValueError("平台元数据构建编号无效")
    if metadata["schemaVersion"] != 1 or metadata["component"] != "fqgate":
        raise ValueError("平台元数据组件无效")
    if metadata["version"] != version or metadata["tag"] != f"fqgate-v{version}":
        raise ValueError("平台元数据版本不一致")
    package = metadata.get("package")
    assets = metadata.get("assets")
    if not isinstance(package, dict) or not isinstance(assets, list):
        raise ValueError("平台元数据缺少 package 或 assets")
    target = (package.get("platform"), package.get("architecture"), package.get("installMode"))
    if target not in EXPECTED_TARGETS:
        raise ValueError(f"不支持的平台元数据目标：{target}")
    package_asset = {key: package.get(key) for key in ("fileName", "size", "sha256")}
    if package_asset not in assets:
        raise ValueError("主安装包没有列入平台资产")
    expected_count = 4 if target[0] == "windows" else 2
    if len(assets) != expected_count:
        raise ValueError(f"{target[0]} 平台资产数量不正确")
    return target, package, assets


def validate_checksum(checksum_bytes: bytes, target_name: str, target_digest: str) -> None:
    try:
        line = checksum_bytes.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ValueError(f"SHA-256 文件不是 ASCII：{target_name}.sha256") from error
    match = re.fullmatch(r"([a-fA-F0-9]{64})\s{2}([^/\\]+)", line)
    if not match or match.group(2) != target_name or match.group(1).lower() != target_digest:
        raise ValueError(f"SHA-256 文件内容不匹配：{target_name}.sha256")


def build_candidate(api, repository: str, version: str) -> dict:
    parse_version(version)
    tag = f"fqgate-v{version}"
    release = find_release(api, repository, tag)
    assets = release.get("assets") or []
    by_name = {}
    for asset in assets:
        name = asset.get("name")
        if not isinstance(name, str) or Path(name).name != name or FORBIDDEN_NAME_PATTERN.search(name):
            raise ValueError(f"Release 包含禁止发布的文件：{name}")
        if name in by_name:
            raise ValueError(f"Release 包含重复资产：{name}")
        by_name[name] = asset

    cache = {}

    def download(name: str) -> bytes:
        if name not in by_name:
            raise ValueError(f"Release 缺少资产：{name}")
        if name not in cache:
            cache[name] = api.download_asset(by_name[name])
        return cache[name]

    request_name = f"FQGate-{version}-release-request.json"
    request = read_json_bytes(download(request_name), request_name)
    validate_request(request, version)

    metadata_names = sorted(name for name in by_name if name.endswith(".release.json"))
    if len(metadata_names) != 3:
        raise ValueError("Release 必须包含三个平台元数据文件")
    targets = set()
    described = {}
    packages = []
    for metadata_name in metadata_names:
        metadata = read_json_bytes(download(metadata_name), metadata_name)
        target, package, platform_assets = validate_platform_metadata(metadata, version)
        if target in targets:
            raise ValueError(f"平台元数据重复：{target}")
        targets.add(target)
        packages.append(package)
        for description in platform_assets:
            if not isinstance(description, dict) or set(description) != {"fileName", "size", "sha256"}:
                raise ValueError("平台资产字段不符合约定")
            name = description["fileName"]
            if name in described:
                raise ValueError(f"平台资产被重复描述：{name}")
            content = download(name)
            actual_digest = sha256_bytes(content)
            if len(content) != description["size"] or actual_digest != description["sha256"]:
                raise ValueError(f"平台资产大小或 SHA-256 不匹配：{name}")
            api_asset = by_name[name]
            if api_asset.get("size") != len(content):
                raise ValueError(f"GitHub 记录的资产大小不匹配：{name}")
            api_digest = api_asset.get("digest")
            if api_digest and api_digest != f"sha256:{actual_digest}":
                raise ValueError(f"GitHub 记录的资产摘要不匹配：{name}")
            described[name] = description

    if targets != EXPECTED_TARGETS:
        raise ValueError("Release 的平台和架构不完整")
    expected_names = {request_name, *metadata_names, *described}
    if set(by_name) != expected_names:
        raise ValueError(f"Release 资产集合不符合约定；多余：{sorted(set(by_name) - expected_names)}")
    for name, description in described.items():
        if name.endswith(".sha256"):
            continue
        checksum_name = f"{name}.sha256"
        if checksum_name not in described:
            raise ValueError(f"发行资产缺少校验文件：{checksum_name}")
        validate_checksum(download(checksum_name), name, description["sha256"])

    order = {("windows", "x86_64"): 0, ("macos", "aarch64"): 1, ("macos", "x86_64"): 2}
    packages.sort(key=lambda item: order[(item["platform"], item["architecture"])])
    manifest = {
        "schemaVersion": 1,
        "component": "fqgate",
        "channel": "stable",
        "status": "published" if not release.get("draft") else "unpublished",
        "version": version,
        "publishedAt": release.get("published_at") if not release.get("draft") else None,
        "minimumSupportedVersion": request["minimumSupportedVersion"],
        "releaseNotes": request["releaseNotes"],
        "packages": packages,
    }
    return {
        "schemaVersion": 1,
        "repository": repository,
        "releaseId": release["id"],
        "tag": tag,
        "source": request["source"],
        "manifest": manifest,
    }


def read_json_file(path: Path) -> dict:
    return read_json_bytes(path.read_bytes(), str(path))


def write_json_file(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_stable_not_newer(repository_root: Path, version: str) -> None:
    stable_path = repository_root / "releases" / "stable.json"
    if stable_path.exists():
        stable = read_json_file(stable_path)
        stable_version = str(stable.get("version", ""))
        if parse_version(stable_version) > parse_version(version):
            raise ValueError(f"拒绝把稳定通道从 {stable_version} 降级到 {version}")


def prepare_manifest(candidate: dict, repository_root: Path) -> None:
    manifest = dict(candidate["manifest"])
    version = manifest["version"]
    ensure_stable_not_newer(repository_root, version)
    target = repository_root / "releases" / f"{version}.json"
    if target.exists():
        existing = read_json_file(target)
        if existing.get("status") == "published":
            if existing != manifest:
                raise ValueError("已经发布的版本清单与当前候选不一致")
            return
    manifest["status"] = "unpublished"
    manifest["publishedAt"] = None
    write_json_file(target, manifest)


def release_body(manifest: dict, source: dict) -> str:
    notes = "\n".join(f"- {note}" for note in manifest["releaseNotes"])
    return f"## 本次更新\n\n{notes}\n\n构建来源：`{source['commit']}`"


def publish_candidate(api, candidate: dict) -> dict:
    release = find_release(api, candidate["repository"], candidate["tag"])
    if release["id"] != candidate["releaseId"]:
        raise ValueError("待发布 Release 身份已经变化")
    if release.get("draft"):
        release = api.publish_release(
            candidate["repository"], release["id"], release_body(candidate["manifest"], candidate["source"])
        )
    published_at = release.get("published_at")
    if release.get("draft") or not published_at:
        raise RuntimeError("GitHub Release 没有成功进入已发布状态")
    manifest = dict(candidate["manifest"])
    manifest["status"] = "published"
    manifest["publishedAt"] = published_at
    return manifest


def render_download_section(manifest: dict) -> str:
    version = manifest["version"]
    packages = {(item["platform"], item["architecture"]): item for item in manifest["packages"]}
    windows_exe = packages[("windows", "x86_64")]["fileName"]
    windows_zip = str(Path(windows_exe).with_suffix(".zip"))
    arm = packages[("macos", "aarch64")]["fileName"]
    intel = packages[("macos", "x86_64")]["fileName"]
    return f"""## 下载 FQGate

当前正式版是 [FQGate v{version}](https://github.com/zhuyifang/fqgate-releases/releases/tag/fqgate-v{version})。

- Windows 电脑下载 `{windows_zip}`，解压后运行 `FQGate.exe`；也可以直接下载单文件 EXE。
- Apple 芯片 Mac 下载 `{arm}`。
- Intel 芯片 Mac 下载 `{intel}`。

文件名、大小和 SHA-256 可以在 [稳定版清单](./releases/stable.json)中查看。安装完成后，为 FQGate 创建一个桌面快捷方式，方便以后启动。

> 当前 Windows 程序还没有商业代码签名，macOS 程序也没有经过 Apple 公证，系统可能显示安全提示。请只从本仓库的发行页面下载，并核对页面公布的文件校验值。

"""


def promote_manifest(manifest: dict, repository_root: Path) -> None:
    if manifest.get("status") != "published" or not manifest.get("publishedAt"):
        raise ValueError("只有已经公开的版本才能切换稳定通道")
    version = manifest["version"]
    ensure_stable_not_newer(repository_root, version)
    write_json_file(repository_root / "releases" / f"{version}.json", manifest)
    write_json_file(repository_root / "releases" / "stable.json", manifest)

    readme_path = repository_root / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"(?ms)^## 下载 FQGate\n.*?(?=^## 使用方法\n)",
        render_download_section(manifest),
        readme,
        count=1,
    )
    if count != 1:
        raise ValueError("README.md 缺少唯一的下载章节")
    readme_path.write_text(updated, encoding="utf-8")


def write_github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", "zhuyifang/fqgate-releases"))
    validate_parser.add_argument("--version", required=True)
    validate_parser.add_argument("--output", type=Path, required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--candidate", type=Path, required=True)
    prepare_parser.add_argument("--repository-root", type=Path, default=Path.cwd())

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--candidate", type=Path, required=True)
    publish_parser.add_argument("--output", type=Path, required=True)

    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--manifest", type=Path, required=True)
    promote_parser.add_argument("--repository-root", type=Path, default=Path.cwd())

    args = parser.parse_args(argv)
    if args.command in {"validate", "publish"}:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError("GitHub Release 操作需要 GITHUB_TOKEN")
        api = GitHubApi(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    if args.command == "validate":
        candidate = build_candidate(api, args.repository, args.version)
        write_json_file(args.output, candidate)
        write_github_output("version", args.version)
    elif args.command == "prepare":
        prepare_manifest(read_json_file(args.candidate), args.repository_root.resolve())
    elif args.command == "publish":
        manifest = publish_candidate(api, read_json_file(args.candidate))
        write_json_file(args.output, manifest)
        write_github_output("published_at", manifest["publishedAt"])
    else:
        promote_manifest(read_json_file(args.manifest), args.repository_root.resolve())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"FQGate 正式发布失败：{error}", file=sys.stderr)
        raise SystemExit(1)
