"""FQGate 双源正式发布与在线 freshness 维护。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import io
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path


SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SAFE_ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
FORBIDDEN_NAME_PATTERN = re.compile(r"(?:TEST|DEBUG|UNSIGNED|ADHOC)", re.IGNORECASE)
EXPECTED_TARGETS = {
    ("windows", "x86_64", "replaceApplication"),
    ("macos", "aarch64", "replaceApplication"),
    ("macos", "x86_64", "replaceApplication"),
}
REQUIRED_ENTRIES = {
    "windows": ["FQGate.exe", "fqgate-updater.exe"],
    "macos": [
        "FQGate.app/Contents/MacOS/fqgate",
        "FQGate.app/Contents/MacOS/fqgate-updater",
    ],
}
CURRENT_SOURCE_REPOSITORY = "fqgate/FQGate"
LEGACY_SOURCE_REPOSITORIES = frozenset({"zhuyifang/fqgate"})
ALLOWED_SOURCE_REPOSITORIES = frozenset(
    {CURRENT_SOURCE_REPOSITORY, *LEGACY_SOURCE_REPOSITORIES}
)
DEFAULT_GITHUB_REPOSITORY = "fqgate/FQGate-releases"
DEFAULT_GITEE_REPOSITORY = "qicuo/fqgate-releases"
RELEASE_SIGNING_KEY_ID = "update-release-signing-v1"
FRESHNESS_SIGNING_KEY_ID = "update-freshness-signing-v1"
RELEASE_LIFETIME_SECONDS = 30 * 24 * 60 * 60
FRESHNESS_LIFETIME_SECONDS = 12 * 60 * 60
ROOT_FRESHNESS_PATH = "releases/freshness.json"
ROOT_STABLE_ALIAS_PATH = "releases/stable.json"


def parse_version(value: str) -> tuple[int, int, int]:
    match = SEMVER_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"版本号必须是严格语义版本：{value}")
    return tuple(int(part) for part in match.groups())


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def base64url_decode(value: str, label: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"{label} 不是有效的 Base64URL 无填充编码")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, base64.binascii.Error) as error:
        raise ValueError(f"{label} 不是有效的 Base64URL 无填充编码") from error


def _cryptography():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as error:
        raise RuntimeError("签名操作需要安装 cryptography") from error
    return serialization, Ed25519PrivateKey, Ed25519PublicKey


def load_signing_key(private_value: str, expected_public: str):
    serialization, Ed25519PrivateKey, _ = _cryptography()
    encoded = private_value.strip()
    try:
        if "BEGIN" in encoded:
            key = serialization.load_pem_private_key(
                encoded.encode("utf-8"), password=None
            )
        else:
            raw = base64url_decode(encoded, "签名私钥")
            if len(raw) != 32:
                raise ValueError("签名私钥必须是 32 字节 Ed25519 seed")
            key = Ed25519PrivateKey.from_private_bytes(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("Ed25519 私钥无效") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("升级签名必须使用 Ed25519 私钥")
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if base64url_encode(public_raw) != expected_public.strip():
        raise ValueError("签名私钥与配置的公钥不匹配")
    return key


def sign_document(payload: dict, private_key, key_id: str) -> bytes:
    if key_id not in {RELEASE_SIGNING_KEY_ID, FRESHNESS_SIGNING_KEY_ID}:
        raise ValueError("不支持的升级签名密钥类型")
    signed = canonical_json(payload)
    envelope = {
        "keyId": key_id,
        "signed": base64url_encode(signed),
        "signature": base64url_encode(private_key.sign(signed)),
    }
    return canonical_json(envelope) + b"\n"


def verify_document(document: bytes, public_key: str, expected_key_id: str) -> dict:
    _, _, Ed25519PublicKey = _cryptography()
    envelope = read_json_bytes(document, "签名文档")
    if set(envelope) != {"keyId", "signed", "signature"}:
        raise ValueError("签名文档信封字段不符合约定")
    if envelope["keyId"] != expected_key_id:
        raise ValueError("签名文档使用了错误类型的密钥")
    raw_public = base64url_decode(public_key.strip(), "签名公钥")
    if len(raw_public) != 32:
        raise ValueError("签名公钥必须是 32 字节")
    signed = base64url_decode(envelope["signed"], "签名正文")
    signature = base64url_decode(envelope["signature"], "签名值")
    try:
        Ed25519PublicKey.from_public_bytes(raw_public).verify(signature, signed)
    except Exception as error:
        raise ValueError("签名文档验签失败") from error
    return read_json_bytes(signed, "签名正文")


def read_json_bytes(value: bytes, label: str) -> dict:
    try:
        result = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} 不是有效的 UTF-8 JSON：{error}") from error
    if not isinstance(result, dict):
        raise ValueError(f"{label} 根节点必须是对象")
    return result


def write_json_file(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


class GitHubApi:
    def __init__(
        self,
        token: str,
        repository: str = DEFAULT_GITHUB_REPOSITORY,
        api_root: str = "https://api.github.com",
    ):
        self.token = token
        self.repository = repository
        self.api_root = api_root.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        value=None,
        accept: str = "application/vnd.github+json",
        missing_ok: bool = False,
    ) -> bytes | None:
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
                return response.read()
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub API {method} {urllib.parse.urlsplit(url).path} 返回 {error.code}：{detail}"
            ) from error

    def json_request(self, method: str, path: str, value=None, missing_ok=False):
        payload = self.request(method, path, value, missing_ok=missing_ok)
        return None if payload is None else json.loads(payload) if payload else None

    def list_releases(self, _repository: str | None = None) -> list[dict]:
        return self.json_request(
            "GET", f"/repos/{self.repository}/releases?per_page=100"
        )

    def release_by_tag(self, tag: str) -> dict | None:
        matches = [
            release
            for release in self.list_releases()
            if release.get("tag_name") == tag
        ]
        if len(matches) > 1:
            raise ValueError(f"GitHub 存在重复 Release：{tag}")
        return matches[0] if matches else None

    def assets(self, release: dict) -> list[dict]:
        return self.json_request(
            "GET",
            f"/repos/{self.repository}/releases/{release['id']}/assets?per_page=100",
        )

    def download_asset(self, asset: dict) -> bytes:
        return self.request("GET", asset["url"], accept="application/octet-stream")

    def publish_release(self, release: dict, body: str) -> dict:
        return self.json_request(
            "PATCH",
            f"/repos/{self.repository}/releases/{release['id']}",
            {
                "tag_name": release["tag_name"],
                "body": body,
                "draft": False,
                "prerelease": False,
                "make_latest": "true",
            },
        )

    def read_content(self, path: str) -> bytes | None:
        encoded_path = urllib.parse.quote(path, safe="/")
        value = self.json_request(
            "GET",
            f"/repos/{self.repository}/contents/{encoded_path}?ref=main",
            missing_ok=True,
        )
        if value is None:
            return None
        return base64.b64decode(value["content"])

    def write_content(self, path: str, content: bytes, message: str) -> None:
        encoded_path = urllib.parse.quote(path, safe="/")
        current = self.json_request(
            "GET",
            f"/repos/{self.repository}/contents/{encoded_path}?ref=main",
            missing_ok=True,
        )
        value = {
            "message": message,
            "branch": "main",
            "content": base64.b64encode(content).decode("ascii"),
        }
        if current:
            value["sha"] = current["sha"]
        self.json_request(
            "PUT", f"/repos/{self.repository}/contents/{encoded_path}", value
        )

    def delete_content(self, path: str, message: str) -> None:
        encoded_path = urllib.parse.quote(path, safe="/")
        current = self.json_request(
            "GET",
            f"/repos/{self.repository}/contents/{encoded_path}?ref=main",
            missing_ok=True,
        )
        if current:
            self.json_request(
                "DELETE",
                f"/repos/{self.repository}/contents/{encoded_path}",
                {"message": message, "branch": "main", "sha": current["sha"]},
            )


class GiteeApi:
    def __init__(
        self,
        token: str,
        repository: str = DEFAULT_GITEE_REPOSITORY,
        api_root: str = "https://gitee.com/api/v5",
    ):
        self.token = token
        self.repository = repository
        self.owner, self.repo = repository.split("/", 1)
        self.api_root = api_root.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        fields=None,
        accept: str = "application/json",
        missing_ok: bool = False,
    ) -> bytes | None:
        separator = "&" if "?" in path else "?"
        url = f"{self.api_root}{path}{separator}{urllib.parse.urlencode({'access_token': self.token})}"
        body = None
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "fqgate-release-finalizer",
        }
        if fields is not None:
            body = urllib.parse.urlencode(fields).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            detail = error.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Gitee API {method} {path.split('?', 1)[0]} 返回 {error.code}：{detail}"
            ) from error

    def json_request(self, method: str, path: str, fields=None, missing_ok=False):
        payload = self.request(method, path, fields, missing_ok=missing_ok)
        return None if payload is None else json.loads(payload) if payload else None

    def release_by_tag(self, tag: str) -> dict | None:
        return self.json_request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/releases/tags/{urllib.parse.quote(tag)}",
            missing_ok=True,
        )

    def get_or_create_release(self, tag: str, body: str) -> dict:
        release = self.release_by_tag(tag)
        if release:
            return release
        return self.json_request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/releases",
            {
                "tag_name": tag,
                "target_commitish": "main",
                "name": tag.replace("fqgate-", "FQGate "),
                "body": body,
                "prerelease": "true",
            },
        )

    def assets(self, release: dict) -> list[dict]:
        return self.json_request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/releases/{release['id']}/attach_files",
        )

    def download_asset(self, release: dict, asset: dict) -> bytes:
        return self.request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/releases/{release['id']}/attach_files/{asset['id']}/download",
            accept="application/octet-stream",
        )

    def delete_asset(self, release: dict, asset: dict) -> None:
        self.json_request(
            "DELETE",
            f"/repos/{self.owner}/{self.repo}/releases/{release['id']}/attach_files/{asset['id']}",
        )

    def upload_asset(self, release: dict, name: str, content: bytes) -> dict:
        boundary = f"fqgate-{uuid.uuid4().hex}"
        prefix = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="access_token"\r\n\r\n{self.token}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {mimetypes.guess_type(name)[0] or 'application/octet-stream'}\r\n\r\n"
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        parsed = urllib.parse.urlsplit(self.api_root)
        endpoint = (
            f"{parsed.path}/repos/{self.owner}/{self.repo}/releases/"
            f"{release['id']}/attach_files"
        )
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port, timeout=120
        )
        connection.putrequest("POST", endpoint)
        connection.putheader("Authorization", f"Bearer {self.token}")
        connection.putheader(
            "Content-Type", f"multipart/form-data; boundary={boundary}"
        )
        connection.putheader(
            "Content-Length", str(len(prefix) + len(content) + len(suffix))
        )
        connection.endheaders()
        connection.send(prefix)
        connection.send(content)
        connection.send(suffix)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        if response.status >= 400:
            raise RuntimeError(f"Gitee 资产上传失败：HTTP {response.status}")
        return json.loads(payload)

    def set_prerelease(self, release: dict, prerelease: bool) -> dict:
        required = {
            key: release.get(key)
            for key in ("tag_name", "target_commitish", "name", "body")
        }
        if not all(isinstance(value, str) and value for value in required.values()):
            raise RuntimeError("Gitee Release 缺少变更公开状态所需字段")
        return self.json_request(
            "PATCH",
            f"/repos/{self.owner}/{self.repo}/releases/{release['id']}",
            {**required, "prerelease": "true" if prerelease else "false"},
        )

    def read_content(self, path: str) -> bytes | None:
        encoded_path = urllib.parse.quote(path, safe="/")
        value = self.json_request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/contents/{encoded_path}?ref=main",
            missing_ok=True,
        )
        if value is None:
            return None
        return base64.b64decode(value["content"])

    def write_content(self, path: str, content: bytes, message: str) -> None:
        encoded_path = urllib.parse.quote(path, safe="/")
        current = self.json_request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/contents/{encoded_path}?ref=main",
            missing_ok=True,
        )
        fields = {
            "message": message,
            "branch": "main",
            "content": base64.b64encode(content).decode("ascii"),
        }
        method = "POST"
        if current:
            method = "PUT"
            fields["sha"] = current["sha"]
        self.json_request(
            method,
            f"/repos/{self.owner}/{self.repo}/contents/{encoded_path}",
            fields,
        )

    def delete_content(self, path: str, message: str) -> None:
        encoded_path = urllib.parse.quote(path, safe="/")
        current = self.json_request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/contents/{encoded_path}?ref=main",
            missing_ok=True,
        )
        if current:
            self.json_request(
                "DELETE",
                f"/repos/{self.owner}/{self.repo}/contents/{encoded_path}",
                {"message": message, "branch": "main", "sha": current["sha"]},
            )


def find_github_release(api, repository: str, tag: str) -> dict:
    matches = [
        release
        for release in api.list_releases(repository)
        if release.get("tag_name") == tag
    ]
    if len(matches) != 1:
        raise ValueError(f"GitHub 必须存在且只能存在一个 {tag} Release")
    release = matches[0]
    if release.get("prerelease"):
        raise ValueError("稳定通道不能使用预发行 GitHub Release")
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
    if (
        request["schemaVersion"] != 1
        or request["component"] != "fqgate-release-request"
    ):
        raise ValueError("发行请求组件不正确")
    if request["version"] != version or request["tag"] != f"fqgate-v{version}":
        raise ValueError("发行请求版本不一致")
    source = request.get("source")
    if not isinstance(source, dict) or set(source) != {"repository", "commit"}:
        raise ValueError("发行请求缺少源码身份")
    if source["repository"] not in ALLOWED_SOURCE_REPOSITORIES or not re.fullmatch(
        r"[0-9a-f]{40}", source["commit"]
    ):
        raise ValueError("发行请求源码身份无效")
    if parse_version(str(request["minimumSupportedVersion"])) > parse_version(version):
        raise ValueError("最低支持版本不能高于发行版本")
    notes = request.get("releaseNotes")
    if not isinstance(notes, list) or not 1 <= len(notes) <= 8:
        raise ValueError("发行请求必须包含 1 到 8 条更新说明")
    if any(
        not isinstance(note, str) or not note.strip() or len(note) > 200
        for note in notes
    ):
        raise ValueError("发行请求包含无效的更新说明")


def validate_platform_metadata(
    metadata: dict, version: str
) -> tuple[tuple[str, str, str], dict, dict, list[dict]]:
    required = {
        "schemaVersion",
        "component",
        "version",
        "tag",
        "package",
        "updatePayload",
        "assets",
    }
    if set(metadata) - {"buildId"} != required:
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
    update_payload = metadata.get("updatePayload")
    assets = metadata.get("assets")
    if (
        not isinstance(package, dict)
        or not isinstance(update_payload, dict)
        or not isinstance(assets, list)
    ):
        raise ValueError("平台元数据缺少 package、updatePayload 或 assets")
    if set(package) != {
        "platform",
        "architecture",
        "installMode",
        "fileName",
        "size",
        "sha256",
    }:
        raise ValueError("平台 package 字段不符合约定")
    target = (
        package["platform"],
        package["architecture"],
        package["installMode"],
    )
    if target not in EXPECTED_TARGETS:
        raise ValueError(f"不支持的平台元数据目标：{target}")
    if not str(package["fileName"]).endswith(".zip"):
        raise ValueError("在线升级主包必须是 ZIP")
    expected_payload = {
        "schemaVersion": 1,
        "format": "zip",
        "requiredEntries": REQUIRED_ENTRIES[package["platform"]],
    }
    if update_payload != expected_payload:
        raise ValueError("平台 updatePayload 契约无效")
    package_asset = {key: package[key] for key in ("fileName", "size", "sha256")}
    if package_asset not in assets:
        raise ValueError("主升级 ZIP 没有列入平台资产")
    expected_count = 4 if target[0] == "windows" else 2
    if len(assets) != expected_count:
        raise ValueError(f"{target[0]} 平台资产数量不正确")
    return target, package, update_payload, assets


def validate_checksum(
    checksum_bytes: bytes, target_name: str, target_digest: str
) -> None:
    try:
        line = checksum_bytes.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ValueError(f"SHA-256 文件不是 ASCII：{target_name}.sha256") from error
    match = re.fullmatch(r"([a-fA-F0-9]{64})\s{2}([^/\\]+)", line)
    if (
        not match
        or match.group(2) != target_name
        or match.group(1).lower() != target_digest
    ):
        raise ValueError(f"SHA-256 文件内容不匹配：{target_name}.sha256")


def validate_update_bundle_bytes(content: bytes, platform: str, name: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = {}
            for item in archive.infolist():
                if item.is_dir():
                    continue
                normalized = item.filename.replace("\\", "/")
                parts = normalized.split("/")
                if (
                    not normalized
                    or normalized.startswith("/")
                    or any(part in {"", ".", ".."} or ":" in part for part in parts)
                ):
                    raise ValueError(f"更新 ZIP 包含不安全路径：{item.filename}")
                if normalized in entries:
                    raise ValueError(f"更新 ZIP 包含重复路径：{normalized}")
                entries[normalized] = item
    except zipfile.BadZipFile as error:
        raise ValueError(f"主升级包不是有效 ZIP：{name}") from error
    required = REQUIRED_ENTRIES[platform]
    missing = [
        required_name
        for required_name in required
        if required_name not in entries or entries[required_name].file_size <= 0
    ]
    if missing:
        raise ValueError(f"主升级 ZIP 缺少必需且非空的入口：{missing}")
    symlinks = [
        required_name
        for required_name in required
        if (entries[required_name].external_attr >> 16) & 0o170000 == 0o120000
    ]
    if symlinks:
        raise ValueError(f"主升级 ZIP 的必需入口不得是符号链接：{symlinks}")


def collect_candidate(
    api, repository: str, version: str
) -> tuple[dict, dict[str, bytes]]:
    parse_version(version)
    tag = f"fqgate-v{version}"
    release = find_github_release(api, repository, tag)
    assets = (
        api.assets(release) if hasattr(api, "assets") else release.get("assets", [])
    )
    by_name = {}
    for asset in assets:
        name = asset.get("name")
        if (
            not isinstance(name, str)
            or not SAFE_ASSET_NAME.fullmatch(name)
            or Path(name).name != name
            or FORBIDDEN_NAME_PATTERN.search(name)
        ):
            raise ValueError(f"Release 包含禁止发布的文件：{name}")
        if name in by_name:
            raise ValueError(f"Release 包含重复资产：{name}")
        by_name[name] = asset

    cache: dict[str, bytes] = {}

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
        target, package, update_payload, platform_assets = validate_platform_metadata(
            metadata, version
        )
        if target in targets:
            raise ValueError(f"平台元数据重复：{target}")
        targets.add(target)
        packages.append(
            {
                "platform": package["platform"],
                "architecture": package["architecture"],
                "package": {
                    key: package[key] for key in ("fileName", "size", "sha256")
                },
                "requiredEntries": update_payload["requiredEntries"],
            }
        )
        for description in platform_assets:
            if not isinstance(description, dict) or set(description) != {
                "fileName",
                "size",
                "sha256",
            }:
                raise ValueError("平台资产字段不符合约定")
            name = description["fileName"]
            if name in described:
                raise ValueError(f"平台资产被重复描述：{name}")
            content = download(name)
            actual_digest = sha256_bytes(content)
            if (
                len(content) != description["size"]
                or actual_digest != description["sha256"]
            ):
                raise ValueError(f"平台资产大小或 SHA-256 不匹配：{name}")
            api_asset = by_name[name]
            if api_asset.get("size") != len(content):
                raise ValueError(f"GitHub 记录的资产大小不匹配：{name}")
            api_digest = api_asset.get("digest")
            if api_digest and api_digest != f"sha256:{actual_digest}":
                raise ValueError(f"GitHub 记录的资产摘要不匹配：{name}")
            described[name] = description
        validate_update_bundle_bytes(
            download(package["fileName"]), package["platform"], package["fileName"]
        )

    if targets != EXPECTED_TARGETS:
        raise ValueError("Release 的平台和架构不完整")
    expected_names = {request_name, *metadata_names, *described}
    if set(by_name) != expected_names:
        raise ValueError(
            f"Release 资产集合不符合约定；多余：{sorted(set(by_name) - expected_names)}"
        )
    for name, description in described.items():
        if name.endswith(".sha256"):
            continue
        checksum_name = f"{name}.sha256"
        if checksum_name not in described:
            raise ValueError(f"发行资产缺少校验文件：{checksum_name}")
        validate_checksum(download(checksum_name), name, description["sha256"])

    order = {("windows", "x86_64"): 0, ("macos", "aarch64"): 1, ("macos", "x86_64"): 2}
    packages.sort(key=lambda item: order[(item["platform"], item["architecture"])])
    candidate = {
        "schemaVersion": 1,
        "repository": repository,
        "releaseId": release["id"],
        "tag": tag,
        "draft": bool(release.get("draft")),
        "publishedAt": release.get("published_at"),
        "source": request["source"],
        "minimumSupportedVersion": request["minimumSupportedVersion"],
        "releaseNotes": request["releaseNotes"],
        "packages": packages,
        "assets": [
            {
                "fileName": name,
                "size": len(download(name)),
                "sha256": sha256_bytes(download(name)),
            }
            for name in sorted(by_name)
        ],
    }
    return candidate, cache


def build_candidate(api, repository: str, version: str) -> dict:
    return collect_candidate(api, repository, version)[0]


def release_body(candidate: dict) -> str:
    notes = "\n".join(f"- {note}" for note in candidate["releaseNotes"])
    return f"## 本次更新\n\n{notes}\n\n构建来源：`{candidate['source']['commit']}`"


def asset_name(asset: dict) -> str:
    name = asset.get("name", asset.get("filename"))
    if not isinstance(name, str):
        raise RuntimeError("发行通道返回了没有名称的资产")
    return name


def mirror_to_gitee(
    gitee, candidate: dict, contents: dict[str, bytes]
) -> tuple[dict, dict[str, dict]]:
    release = gitee.get_or_create_release(candidate["tag"], release_body(candidate))
    existing = {asset_name(asset): asset for asset in gitee.assets(release)}
    expected_names = {item["fileName"] for item in candidate["assets"]}
    public = not bool(release.get("prerelease"))
    extras = set(existing) - expected_names
    if extras and public:
        raise RuntimeError(f"Gitee 已公开 Release 含有多余资产：{sorted(extras)}")
    for name in sorted(extras):
        gitee.delete_asset(release, existing.pop(name))

    mirrored = {}
    for description in candidate["assets"]:
        name = description["fileName"]
        expected = contents[name]
        asset = existing.get(name)
        if asset:
            actual = gitee.download_asset(release, asset)
            if actual != expected or sha256_bytes(actual) != description["sha256"]:
                if public:
                    raise RuntimeError(f"Gitee 已公开资产与 GitHub 不一致：{name}")
                gitee.delete_asset(release, asset)
                asset = None
        if asset is None:
            if public:
                raise RuntimeError(f"Gitee 已公开 Release 缺少资产：{name}")
            asset = gitee.upload_asset(release, name, expected)
        actual = gitee.download_asset(release, asset)
        if actual != expected or sha256_bytes(actual) != description["sha256"]:
            raise RuntimeError(f"Gitee 资产上传后回读不一致：{name}")
        mirrored[name] = asset
    return release, mirrored


def publish_release_pair(
    github, gitee, candidate: dict, gitee_release: dict
) -> tuple[dict, dict]:
    tag = candidate["tag"]
    github_release = github.release_by_tag(tag)
    if github_release is None or github_release["id"] != candidate["releaseId"]:
        raise RuntimeError("GitHub 待发布 Release 身份已经变化")
    body = release_body(candidate)

    if gitee_release.get("prerelease"):
        try:
            gitee_release = gitee.set_prerelease(gitee_release, False)
        except Exception:
            observed = gitee.release_by_tag(tag)
            if not observed or observed.get("prerelease"):
                raise
            gitee_release = observed
    if gitee_release.get("prerelease"):
        raise RuntimeError("Gitee Release 没有成功公开")

    if github_release.get("draft"):
        try:
            github_release = github.publish_release(github_release, body)
        except Exception as publish_error:
            observed = github.release_by_tag(tag)
            if observed and not observed.get("draft") and observed.get("published_at"):
                github_release = observed
            else:
                try:
                    gitee.set_prerelease(gitee_release, True)
                except Exception as rollback_error:
                    raise RuntimeError(
                        "GitHub 公开失败，且 Gitee 未能恢复为预发布状态"
                    ) from rollback_error
                raise publish_error
    if github_release.get("draft") or not github_release.get("published_at"):
        try:
            gitee.set_prerelease(gitee_release, True)
        except Exception as rollback_error:
            raise RuntimeError(
                "GitHub Release 未进入公开状态，且 Gitee 未能恢复为预发布状态"
            ) from rollback_error
        raise RuntimeError("GitHub Release 没有成功公开")
    return github_release, gitee_release


def parse_github_timestamp(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("GitHub Release 缺少实际公开时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("GitHub Release 公开时间格式无效") from error
    if parsed.tzinfo is None:
        raise ValueError("GitHub Release 公开时间必须含时区")
    result = int(parsed.timestamp())
    if result <= 0:
        raise ValueError("GitHub Release 公开时间无效")
    return result


def gitee_download_url(asset: dict) -> str:
    value = asset.get("browser_download_url", asset.get("download_url"))
    if not isinstance(value, str):
        raise RuntimeError(f"Gitee 资产缺少下载地址：{asset_name(asset)}")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "gitee.com":
        raise RuntimeError(f"Gitee 资产下载地址不安全：{asset_name(asset)}")
    return value


def build_release_payload(
    candidate: dict,
    mirrored: dict[str, dict],
    sequence: int,
    published_at: int,
) -> dict:
    artifacts = []
    for item in candidate["packages"]:
        package = item["package"]
        name = package["fileName"]
        artifacts.append(
            {
                "platform": item["platform"],
                "arch": item["architecture"],
                "fileName": name,
                "installMode": "replaceApplication",
                "updatePayload": {
                    "schemaVersion": 1,
                    "format": "zip",
                    "requiredEntries": item["requiredEntries"],
                },
                "downloadUrls": {
                    "github": (
                        f"https://github.com/{candidate['repository']}/releases/download/"
                        f"{candidate['tag']}/{urllib.parse.quote(name)}"
                    ),
                    "gitee": gitee_download_url(mirrored[name]),
                },
                "bytes": package["size"],
                "sha256": package["sha256"],
            }
        )
    return {
        "schemaVersion": 1,
        "channel": "stable",
        "sequence": sequence,
        "version": candidate["tag"].removeprefix("fqgate-v"),
        "publishedAt": published_at,
        "expiresAt": published_at + RELEASE_LIFETIME_SECONDS,
        "releaseNotes": candidate["releaseNotes"],
        "artifacts": artifacts,
    }


def build_legacy_stable_payload(candidate: dict, published_at: str) -> dict:
    """Build the exact flat manifest consumed by every 1.x updater."""
    descriptions = {asset["fileName"]: asset for asset in candidate["assets"]}
    packages = []
    for item in candidate["packages"]:
        package = item["package"]
        if item["platform"] == "windows":
            file_name = f"{Path(package['fileName']).stem}.exe"
            install_mode = "replaceExecutable"
        else:
            file_name = package["fileName"]
            install_mode = "openPackage"
        description = descriptions.get(file_name)
        if description is None:
            raise ValueError(f"1.x 兼容更新缺少发行资产：{file_name}")
        packages.append(
            {
                "platform": item["platform"],
                "architecture": item["architecture"],
                "installMode": install_mode,
                "fileName": file_name,
                "size": description["size"],
                "sha256": description["sha256"],
            }
        )
    return {
        "schemaVersion": 1,
        "component": "fqgate",
        "channel": "stable",
        "status": "published",
        "version": candidate["tag"].removeprefix("fqgate-v"),
        "publishedAt": published_at,
        "minimumSupportedVersion": candidate["minimumSupportedVersion"],
        "releaseNotes": candidate["releaseNotes"],
        "packages": packages,
    }


def validate_stable_payload(payload: dict) -> None:
    if set(payload) != {
        "schemaVersion",
        "channel",
        "sequence",
        "version",
        "releasePath",
        "releaseSha256",
    }:
        raise ValueError("stable 正文字段不符合约定")
    if (
        payload["schemaVersion"] != 1
        or payload["channel"] != "stable"
        or type(payload["sequence"]) is not int
        or payload["sequence"] <= 0
        or parse_version(payload["version"]) < (0, 0, 0)
        or payload["releasePath"] != f"{payload['version']}/release.json"
        or not re.fullmatch(r"[0-9a-f]{64}", payload["releaseSha256"])
    ):
        raise ValueError("stable 正文内容无效")


def validate_freshness_payload(payload: dict) -> None:
    if set(payload) != {
        "schemaVersion",
        "channel",
        "refreshSequence",
        "issuedAt",
        "expiresAt",
        "stablePath",
        "stableSha256",
    }:
        raise ValueError("freshness 正文字段不符合约定")
    if (
        payload["schemaVersion"] != 1
        or payload["channel"] != "stable"
        or type(payload["refreshSequence"]) is not int
        or payload["refreshSequence"] <= 0
        or type(payload["issuedAt"]) is not int
        or type(payload["expiresAt"]) is not int
        or payload["issuedAt"] >= payload["expiresAt"]
        or payload["expiresAt"] - payload["issuedAt"] != FRESHNESS_LIFETIME_SECONDS
        or not re.fullmatch(r"[1-9]\d*/stable\.json", payload["stablePath"])
        or not re.fullmatch(r"[0-9a-f]{64}", payload["stableSha256"])
    ):
        raise ValueError("freshness 正文内容无效")


def validate_release_payload(payload: dict) -> None:
    if set(payload) != {
        "schemaVersion",
        "channel",
        "sequence",
        "version",
        "publishedAt",
        "expiresAt",
        "releaseNotes",
        "artifacts",
    }:
        raise ValueError("release 正文字段不符合约定")
    if (
        payload["schemaVersion"] != 1
        or payload["channel"] != "stable"
        or type(payload["sequence"]) is not int
        or payload["sequence"] <= 0
        or parse_version(payload["version"]) < (0, 0, 0)
        or type(payload["publishedAt"]) is not int
        or type(payload["expiresAt"]) is not int
        or payload["publishedAt"] + RELEASE_LIFETIME_SECONDS != payload["expiresAt"]
        or not isinstance(payload["releaseNotes"], list)
        or not all(isinstance(note, str) and note for note in payload["releaseNotes"])
        or not isinstance(payload["artifacts"], list)
        or len(payload["artifacts"]) != 3
    ):
        raise ValueError("release 正文内容无效")
    targets = set()
    for artifact in payload["artifacts"]:
        if not isinstance(artifact, dict) or set(artifact) != {
            "platform",
            "arch",
            "fileName",
            "installMode",
            "updatePayload",
            "downloadUrls",
            "bytes",
            "sha256",
        }:
            raise ValueError("release artifact 字段不符合约定")
        target = (artifact["platform"], artifact["arch"])
        if target in targets or target not in {
            ("windows", "x86_64"),
            ("macos", "aarch64"),
            ("macos", "x86_64"),
        }:
            raise ValueError("release artifact 平台或架构无效")
        targets.add(target)
        update_payload = artifact["updatePayload"]
        if update_payload != {
            "schemaVersion": 1,
            "format": "zip",
            "requiredEntries": REQUIRED_ENTRIES[artifact["platform"]],
        }:
            raise ValueError("release artifact updatePayload 无效")
        if (
            artifact["installMode"] != "replaceApplication"
            or not isinstance(artifact["fileName"], str)
            or not SAFE_ASSET_NAME.fullmatch(artifact["fileName"])
            or not artifact["fileName"].endswith(".zip")
            or type(artifact["bytes"]) is not int
            or artifact["bytes"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
            or not isinstance(artifact["downloadUrls"], dict)
            or set(artifact["downloadUrls"]) != {"github", "gitee"}
        ):
            raise ValueError("release artifact 内容无效")
        for source, url in artifact["downloadUrls"].items():
            parsed = urllib.parse.urlsplit(url)
            expected_host = "github.com" if source == "github" else "gitee.com"
            if (
                parsed.scheme != "https"
                or parsed.hostname != expected_host
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("release artifact 下载地址无效")


def read_identical(channels: tuple, path: str) -> bytes | None:
    left = channels[0].read_content(path)
    right = channels[1].read_content(path)
    if left != right:
        raise RuntimeError(f"GitHub 与 Gitee 的 {path} 不一致")
    return left


def read_active_state(
    github, gitee, release_public_key: str, freshness_public_key: str
) -> dict | None:
    freshness_bytes = read_identical((github, gitee), ROOT_FRESHNESS_PATH)
    if freshness_bytes is None:
        return None
    freshness = verify_document(
        freshness_bytes, freshness_public_key, FRESHNESS_SIGNING_KEY_ID
    )
    validate_freshness_payload(freshness)
    stable_path = f"releases/{freshness['stablePath']}"
    stable_bytes = read_identical((github, gitee), stable_path)
    if stable_bytes is None or sha256_bytes(stable_bytes) != freshness["stableSha256"]:
        raise RuntimeError("freshness 指向的 stable 文档缺失或摘要不匹配")
    stable = verify_document(stable_bytes, release_public_key, RELEASE_SIGNING_KEY_ID)
    validate_stable_payload(stable)
    if freshness["stablePath"] != f"{stable['sequence']}/stable.json":
        raise RuntimeError("freshness 与 stable 发布序列不一致")
    release_path = f"releases/{stable['releasePath']}"
    release_bytes = read_identical((github, gitee), release_path)
    if release_bytes is None or sha256_bytes(release_bytes) != stable["releaseSha256"]:
        raise RuntimeError("stable 指向的 release 文档缺失或摘要不匹配")
    release = verify_document(release_bytes, release_public_key, RELEASE_SIGNING_KEY_ID)
    validate_release_payload(release)
    if (
        release.get("sequence") != stable["sequence"]
        or release.get("version") != stable["version"]
    ):
        raise RuntimeError("stable 与 release 身份不一致")
    return {
        "freshness": freshness,
        "freshnessBytes": freshness_bytes,
        "stable": stable,
        "stableBytes": stable_bytes,
        "release": release,
        "releaseBytes": release_bytes,
    }


def checked_write(channel, path: str, content: bytes, message: str) -> None:
    channel.write_content(path, content, message)
    if channel.read_content(path) != content:
        raise RuntimeError(f"发行通道写入后回读不一致：{path}")


def restore_content(
    channel,
    path: str,
    previous: bytes | None,
    expected_current: bytes,
    message: str,
) -> None:
    current = channel.read_content(path)
    if current == previous:
        return
    if current != expected_current:
        raise RuntimeError(f"{path} 回滚前已被其他操作修改，拒绝覆盖")
    if previous is None:
        channel.delete_content(path, message)
    else:
        channel.write_content(path, previous, message)
    if channel.read_content(path) != previous:
        raise RuntimeError(f"发行通道回滚后回读不一致：{path}")


def deploy_immutable(github, gitee, path: str, content: bytes, message: str) -> None:
    previous = {"github": github.read_content(path), "gitee": gitee.read_content(path)}
    for name, value in previous.items():
        if value is not None and value != content:
            raise RuntimeError(f"{name} 已存在不同的不可变文档：{path}")
    written = []
    try:
        for name, channel in (("gitee", gitee), ("github", github)):
            if previous[name] is None:
                written.append((name, channel))
                checked_write(channel, path, content, message)
    except Exception as error:
        rollback_errors = []
        for name, channel in reversed(written):
            try:
                restore_content(
                    channel,
                    path,
                    None,
                    content,
                    f"回滚未完成的 {message}",
                )
            except Exception as rollback_error:
                rollback_errors.append(f"{name}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                f"{path} 部署失败且回滚不完整：{'; '.join(rollback_errors)}"
            ) from error
        raise


def switch_freshness(
    github,
    gitee,
    content: bytes,
    expected_previous: bytes | None,
    message: str,
) -> None:
    current = {
        "github": github.read_content(ROOT_FRESHNESS_PATH),
        "gitee": gitee.read_content(ROOT_FRESHNESS_PATH),
    }
    if any(value != expected_previous for value in current.values()):
        raise RuntimeError("freshness 在发布期间发生变化，拒绝覆盖")
    try:
        checked_write(gitee, ROOT_FRESHNESS_PATH, content, message)
        checked_write(github, ROOT_FRESHNESS_PATH, content, message)
    except Exception as error:
        rollback_errors = []
        for name, channel in (("github", github), ("gitee", gitee)):
            try:
                restore_content(
                    channel,
                    ROOT_FRESHNESS_PATH,
                    expected_previous,
                    content,
                    f"回滚未完成的 {message}",
                )
            except Exception as rollback_error:
                rollback_errors.append(f"{name}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                "freshness 切换失败且跨源回滚不完整：" + "; ".join(rollback_errors)
            ) from error
        raise


def switch_release_entrypoints(
    github,
    gitee,
    legacy_stable_bytes: bytes,
    expected_legacy_stable: bytes | None,
    freshness_bytes: bytes,
    expected_freshness: bytes | None,
    message: str,
) -> None:
    current_legacy = read_identical((github, gitee), ROOT_STABLE_ALIAS_PATH)
    if current_legacy != expected_legacy_stable:
        raise RuntimeError("1.x stable 在发布期间发生变化，拒绝覆盖")
    try:
        checked_write(gitee, ROOT_STABLE_ALIAS_PATH, legacy_stable_bytes, message)
        checked_write(github, ROOT_STABLE_ALIAS_PATH, legacy_stable_bytes, message)
        switch_freshness(
            github,
            gitee,
            freshness_bytes,
            expected_freshness,
            message,
        )
    except Exception as error:
        rollback_errors = []
        for name, channel in (("github", github), ("gitee", gitee)):
            try:
                restore_content(
                    channel,
                    ROOT_STABLE_ALIAS_PATH,
                    expected_legacy_stable,
                    legacy_stable_bytes,
                    f"回滚未完成的 {message}",
                )
            except Exception as rollback_error:
                rollback_errors.append(f"{name}: {rollback_error}")
        if rollback_errors:
            raise RuntimeError(
                "稳定入口切换失败且 1.x 通道回滚不完整："
                + "; ".join(rollback_errors)
            ) from error
        raise


def finalize_release(
    *,
    github,
    gitee,
    version: str,
    release_private_key,
    freshness_private_key,
    release_public_key: str,
    freshness_public_key: str,
    now: int | None = None,
) -> dict:
    candidate, contents = collect_candidate(github, github.repository, version)
    active = read_active_state(github, gitee, release_public_key, freshness_public_key)
    previous_legacy_stable = read_identical(
        (github, gitee), ROOT_STABLE_ALIAS_PATH
    )
    if active and active["stable"]["version"] == version:
        return {
            "status": "already_active",
            "version": version,
            "sequence": active["stable"]["sequence"],
            "refreshSequence": active["freshness"]["refreshSequence"],
        }
    if active and parse_version(version) <= parse_version(active["stable"]["version"]):
        raise ValueError(
            f"新稳定版本 {version} 必须高于当前版本 {active['stable']['version']}"
        )
    sequence = 1 if active is None else active["stable"]["sequence"] + 1
    refresh_sequence = (
        1 if active is None else active["freshness"]["refreshSequence"] + 1
    )

    gitee_release, mirrored = mirror_to_gitee(gitee, candidate, contents)
    github_release, _ = publish_release_pair(github, gitee, candidate, gitee_release)
    published_at = parse_github_timestamp(github_release["published_at"])
    release_payload = build_release_payload(candidate, mirrored, sequence, published_at)
    release_bytes = sign_document(
        release_payload, release_private_key, RELEASE_SIGNING_KEY_ID
    )
    validate_release_payload(
        verify_document(release_bytes, release_public_key, RELEASE_SIGNING_KEY_ID)
    )
    release_path = f"releases/{version}/release.json"
    deploy_immutable(
        github,
        gitee,
        release_path,
        release_bytes,
        f"发布 FQGate {version} 不可变 release 文档",
    )

    stable_payload = {
        "schemaVersion": 1,
        "channel": "stable",
        "sequence": sequence,
        "version": version,
        "releasePath": f"{version}/release.json",
        "releaseSha256": sha256_bytes(release_bytes),
    }
    stable_bytes = sign_document(
        stable_payload, release_private_key, RELEASE_SIGNING_KEY_ID
    )
    validate_stable_payload(
        verify_document(stable_bytes, release_public_key, RELEASE_SIGNING_KEY_ID)
    )
    stable_path = f"releases/{sequence}/stable.json"
    deploy_immutable(
        github,
        gitee,
        stable_path,
        stable_bytes,
        f"发布 FQGate {version} 不可变 stable 文档",
    )

    issued_at = int(time.time()) if now is None else now
    freshness_payload = {
        "schemaVersion": 1,
        "channel": "stable",
        "refreshSequence": refresh_sequence,
        "issuedAt": issued_at,
        "expiresAt": issued_at + FRESHNESS_LIFETIME_SECONDS,
        "stablePath": f"{sequence}/stable.json",
        "stableSha256": sha256_bytes(stable_bytes),
    }
    freshness_bytes = sign_document(
        freshness_payload, freshness_private_key, FRESHNESS_SIGNING_KEY_ID
    )
    validate_freshness_payload(
        verify_document(freshness_bytes, freshness_public_key, FRESHNESS_SIGNING_KEY_ID)
    )
    legacy_stable_bytes = canonical_json(
        build_legacy_stable_payload(candidate, github_release["published_at"])
    )
    switch_release_entrypoints(
        github,
        gitee,
        legacy_stable_bytes,
        previous_legacy_stable,
        freshness_bytes,
        None if active is None else active["freshnessBytes"],
        f"切换 FQGate {version} 在线发行入口",
    )
    return {
        "status": "published",
        "version": version,
        "sequence": sequence,
        "refreshSequence": refresh_sequence,
        "publishedAt": published_at,
        "expiresAt": published_at + RELEASE_LIFETIME_SECONDS,
    }


def refresh_freshness(
    *,
    github,
    gitee,
    freshness_private_key,
    release_public_key: str,
    freshness_public_key: str,
    now: int | None = None,
) -> dict:
    active = read_active_state(github, gitee, release_public_key, freshness_public_key)
    if active is None:
        return {"status": "not_initialized"}
    issued_at = int(time.time()) if now is None else now
    payload = {
        "schemaVersion": 1,
        "channel": "stable",
        "refreshSequence": active["freshness"]["refreshSequence"] + 1,
        "issuedAt": issued_at,
        "expiresAt": issued_at + FRESHNESS_LIFETIME_SECONDS,
        "stablePath": active["freshness"]["stablePath"],
        "stableSha256": active["freshness"]["stableSha256"],
    }
    content = sign_document(payload, freshness_private_key, FRESHNESS_SIGNING_KEY_ID)
    validate_freshness_payload(
        verify_document(content, freshness_public_key, FRESHNESS_SIGNING_KEY_ID)
    )
    switch_freshness(
        github,
        gitee,
        content,
        active["freshnessBytes"],
        f"刷新 FQGate 在线证明 {payload['refreshSequence']}",
    )
    return {
        "status": "refreshed",
        "version": active["stable"]["version"],
        "sequence": active["stable"]["sequence"],
        "refreshSequence": payload["refreshSequence"],
        "issuedAt": issued_at,
        "expiresAt": payload["expiresAt"],
    }


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"缺少环境变量：{name}")
    return value


def write_github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            output.write(f"{name}={value}\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--github-repository",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_GITHUB_REPOSITORY),
    )
    parser.add_argument(
        "--gitee-repository",
        default=os.environ.get("GITEE_REPOSITORY", DEFAULT_GITEE_REPOSITORY),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--version", required=True)
    validate_parser.add_argument("--output", type=Path, required=True)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--version", required=True)
    finalize_parser.add_argument("--output", type=Path, required=True)

    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    github = GitHubApi(required_environment("GITHUB_TOKEN"), args.github_repository)
    if args.command == "validate":
        candidate = build_candidate(github, args.github_repository, args.version)
        write_json_file(args.output, candidate)
        write_github_output("version", args.version)
        return 0

    gitee = GiteeApi(required_environment("GITEE_TOKEN"), args.gitee_repository)
    release_public_key = required_environment(
        "FQGATE_UPDATE_RELEASE_SIGNING_V1_PUBLIC_KEY"
    )
    freshness_public_key = required_environment(
        "FQGATE_UPDATE_FRESHNESS_SIGNING_V1_PUBLIC_KEY"
    )
    if args.command == "finalize":
        release_private_key = load_signing_key(
            required_environment("FQGATE_UPDATE_RELEASE_SIGNING_V1_PRIVATE_KEY"),
            release_public_key,
        )
        freshness_private_key = load_signing_key(
            required_environment("FQGATE_UPDATE_FRESHNESS_SIGNING_V1_PRIVATE_KEY"),
            freshness_public_key,
        )
        result = finalize_release(
            github=github,
            gitee=gitee,
            version=args.version,
            release_private_key=release_private_key,
            freshness_private_key=freshness_private_key,
            release_public_key=release_public_key,
            freshness_public_key=freshness_public_key,
        )
    else:
        freshness_private_key = load_signing_key(
            required_environment("FQGATE_UPDATE_FRESHNESS_SIGNING_V1_PRIVATE_KEY"),
            freshness_public_key,
        )
        result = refresh_freshness(
            github=github,
            gitee=gitee,
            freshness_private_key=freshness_private_key,
            release_public_key=release_public_key,
            freshness_public_key=freshness_public_key,
        )
    write_json_file(args.output, result)
    for name in ("version", "sequence", "refreshSequence", "expiresAt"):
        if name in result:
            write_github_output(name, str(result[name]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f"FQGate 正式发布失败：{error}", file=sys.stderr)
        raise SystemExit(1)
