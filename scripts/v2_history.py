"""验签并汇总双源历史，保留污染证据；审计结果不授权恢复或发布。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Callable


V2_ROOT = "releases/v2"
FRESHNESS_PATH = f"{V2_ROOT}/freshness.json"
IN_FLIGHT_PATH = f"{V2_ROOT}/publications/in-flight.json"
VERSION = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
RELEASE_PATH = re.compile(rf"{V2_ROOT}/({VERSION})/release\.json\Z")
STABLE_PATH = re.compile(rf"{V2_ROOT}/([1-9]\d*)/stable\.json\Z")
MANAGEMENT_PATH = re.compile(
    rf"{V2_ROOT}/publications/({VERSION})/(reservation|intent|completion)\.json\Z"
)
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
U64_LIMIT = 2**64


@dataclass(frozen=True)
class HistorySnapshot:
    channel: str
    commit: str
    files: dict[str, bytes]


@dataclass
class HistoryAudit:
    heads: dict[str, str]
    max_sequence: int
    max_refresh_sequence: int
    documents: dict[str, set[bytes]]
    snapshots: list[HistorySnapshot]
    violations: list[str]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("签名 JSON 包含重复字段")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("签名 JSON 包含非标准数字")


def _strict_json(content: bytes):
    return json.loads(
        content.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
    )


def _verified_payload(
    path: str,
    content: bytes,
    release_public_key: str,
    freshness_public_key: str,
    management_validator: Callable[[str, dict], None] | None,
) -> tuple[str, dict]:
    # 延迟导入避免 API 类委托 history 模块时产生 import 循环；不复制签名合同。
    import finalize_v2_release as publisher

    envelope = _strict_json(content)
    if not isinstance(envelope, dict):
        raise ValueError("签名文档信封不是对象")
    _strict_json(publisher.base64url_decode(envelope.get("signed"), "签名正文"))
    release = RELEASE_PATH.fullmatch(path)
    stable = STABLE_PATH.fullmatch(path)
    management = MANAGEMENT_PATH.fullmatch(path)
    if path == FRESHNESS_PATH:
        kind = "freshness"
        payload = publisher.verify_document(
            content, freshness_public_key, publisher.FRESHNESS_SIGNING_KEY_ID
        )
        publisher.validate_freshness_payload(payload)
    else:
        if not (release or stable or management or path == IN_FLIGHT_PATH):
            raise ValueError("V2 历史包含未知文档路径")
        payload = publisher.verify_document(
            content, release_public_key, publisher.RELEASE_SIGNING_KEY_ID
        )
        if release:
            kind = "release"
            # 旧正式链曾把到期时间签进 Release；仅审计其原始合同，不改当前
            # 生产合同，也不改写原 bytes。否则真正旧链会被错当畸形而丢掉证据。
            current_contract = payload
            if "expiresAt" in payload:
                if (
                    type(payload.get("expiresAt")) is not int
                    or type(payload.get("publishedAt")) is not int
                    or payload["expiresAt"] <= payload["publishedAt"]
                ):
                    raise ValueError("历史 Release 到期字段无效")
                current_contract = {
                    key: value for key, value in payload.items() if key != "expiresAt"
                }
            publisher.validate_release_payload(current_contract)
            publisher.validate_v2_version(payload["version"])
            if release[1] != payload["version"]:
                raise ValueError("Release 路径与版本不一致")
        elif stable:
            kind = "stable"
            publisher.validate_stable_payload(payload)
            publisher.validate_v2_version(payload["version"])
            if int(stable[1]) != payload["sequence"]:
                raise ValueError("Stable 路径与序号不一致")
        else:
            kind = "management"
            if management_validator is None:
                raise ValueError("管理文档缺少严格验证回调")
            management_validator(path, payload)
            expected_kind = management[2] if management else "reservation"
            if payload.get("kind") != expected_kind:
                raise ValueError("管理文档路径与 kind 不一致")
            publisher.validate_v2_version(payload["version"])
            if management and management[1] != payload["version"]:
                raise ValueError("管理文档路径与版本不一致")
            if any(
                type(payload.get(field)) is not int or payload[field] <= 0
                for field in ("sequence", "refreshSequence")
            ):
                raise ValueError("管理文档缺少合法预留序号")
    if type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1:
        raise ValueError("文档 schemaVersion 无效")
    for field in (
        "sequence",
        "refreshSequence",
        "publishedAt",
        "issuedAt",
        "expiresAt",
    ):
        if field in payload and (
            type(payload[field]) is not int or not 0 <= payload[field] < U64_LIMIT
        ):
            raise ValueError(f"文档 {field} 超出 u64 合同")
    if kind == "release" and any(
        artifact["bytes"] >= U64_LIMIT for artifact in payload["artifacts"]
    ):
        raise ValueError("Release 资产大小超出 u64 合同")
    return kind, payload


def _link_violations(files: dict, payloads: dict) -> list[str]:
    problems = []
    for path, (kind, payload) in payloads.items():
        if kind == "stable":
            target = f"{V2_ROOT}/{payload['releasePath']}"
            expected = payload["releaseSha256"]
        elif kind == "freshness":
            target = f"{V2_ROOT}/{payload['stablePath']}"
            expected = payload["stableSha256"]
        else:
            continue
        if target not in files:
            problems.append(f"历史断链：{path} 引用缺失的 {target}")
        elif hashlib.sha256(files[target]).hexdigest() != expected:
            problems.append(f"历史摘要不匹配：{path} 引用 {target}")
        elif kind == "stable":
            target_payload = payloads[target][1]
            if (
                payload["sequence"] != target_payload["sequence"]
                or payload["version"] != target_payload["version"]
            ):
                problems.append(f"历史身份不匹配：{path} 引用 {target}")
    return problems


def collect_history(
    github,
    gitee,
    release_public_key: str,
    freshness_public_key: str,
    *,
    management_validator: Callable[[str, dict], None] | None = None,
) -> HistoryAudit:
    """成功返回时，每份单文档已验签并通过字段/路径合同。

    删除、复用、回退和历史断链作为 violations 返回，不能因此忽略其序号。
    API 错误、截断、畸形或验签失败直接抛错；管理预留序号也计入最大值。
    snapshots 只保证各来源内部 oldest → head，不为不同来源伪造时间顺序。
    """
    audit = HistoryAudit({}, 0, 0, {}, [], [])
    verified = {}
    sequence_documents = {"stable": {}, "release": {}, "freshness": {}}
    sequence_versions = {}
    refresh_versions = {}
    current_files = {}
    for channel, api in (("github", github), ("gitee", gitee)):
        head, snapshots = api.history_snapshots()
        if (
            not isinstance(head, str)
            or not SHA_PATTERN.fullmatch(head)
            or not isinstance(snapshots, list)
            or not snapshots
        ):
            raise RuntimeError(f"{channel} 缺少完整钉住的历史")
        audit.heads[channel] = head
        previous_files = {}
        seen_commits = set()
        highest_active_sequence = 0
        highest_active_refresh = 0
        highest_active_version = (0, 0, 0)
        for item in snapshots:
            if not isinstance(item, tuple) or len(item) != 2:
                raise RuntimeError(f"{channel} 历史快照畸形")
            commit, files = item
            if (
                not isinstance(commit, str)
                or not SHA_PATTERN.fullmatch(commit)
                or commit in seen_commits
                or not isinstance(files, dict)
            ):
                raise RuntimeError(f"{channel} 历史快照身份重复或无效")
            seen_commits.add(commit)
            payloads = {}
            for path, content in files.items():
                if not isinstance(path, str) or not isinstance(content, bytes):
                    raise RuntimeError(f"{channel} 历史文档类型无效")
                key = (path, content)
                if key not in verified:
                    try:
                        verified[key] = _verified_payload(
                            path,
                            content,
                            release_public_key,
                            freshness_public_key,
                            management_validator,
                        )
                    except Exception as error:
                        raise RuntimeError(
                            f"{channel}@{commit} 文档验签或合同校验失败：{path}"
                        ) from error
                kind, payload = verified[key]
                payloads[path] = (kind, payload)
                audit.documents.setdefault(path, set()).add(content)
                audit.max_sequence = max(audit.max_sequence, payload.get("sequence", 0))
                audit.max_refresh_sequence = max(
                    audit.max_refresh_sequence, payload.get("refreshSequence", 0)
                )
                if kind in sequence_documents:
                    sequence_field = (
                        "refreshSequence" if kind == "freshness" else "sequence"
                    )
                    sequence_documents[kind].setdefault(
                        payload[sequence_field], set()
                    ).add(content)
                if kind in {"release", "stable", "management"}:
                    sequence_versions.setdefault(payload["sequence"], set()).add(
                        payload["version"]
                    )
                if kind == "management":
                    refresh_versions.setdefault(payload["refreshSequence"], set()).add(
                        payload["version"]
                    )
            for deleted in sorted(set(previous_files) - set(files)):
                if deleted != IN_FLIGHT_PATH:
                    audit.violations.append(
                        f"{channel}@{commit} 删除历史签名文档：{deleted}"
                    )
            for problem in _link_violations(files, payloads):
                audit.violations.append(f"{channel}@{commit} {problem}")
            freshness = payloads.get(FRESHNESS_PATH)
            if freshness:
                fresh_payload = freshness[1]
                refresh = fresh_payload["refreshSequence"]
                if refresh < highest_active_refresh:
                    audit.violations.append(
                        f"{channel}@{commit} Freshness 序号倒退：{refresh} < {highest_active_refresh}"
                    )
                highest_active_refresh = max(highest_active_refresh, refresh)
                stable = payloads.get(f"{V2_ROOT}/{fresh_payload['stablePath']}")
                if stable:
                    import finalize_v2_release as publisher

                    sequence = stable[1]["sequence"]
                    version = publisher.parse_version(stable[1]["version"])
                    refresh_versions.setdefault(refresh, set()).add(
                        stable[1]["version"]
                    )
                    if (
                        sequence < highest_active_sequence
                        or version < highest_active_version
                    ):
                        audit.violations.append(f"{channel}@{commit} 当前稳定入口倒退")
                    highest_active_sequence = max(highest_active_sequence, sequence)
                    highest_active_version = max(highest_active_version, version)
            snapshot = HistorySnapshot(channel, commit, dict(files))
            audit.snapshots.append(snapshot)
            previous_files = files
        if snapshots[-1][0] != head:
            raise RuntimeError(f"{channel} 历史未覆盖钉住的 main HEAD")
        current_files[channel] = dict(previous_files)

    for path, contents in sorted(audit.documents.items()):
        if path not in {FRESHNESS_PATH, IN_FLIGHT_PATH} and len(contents) > 1:
            audit.violations.append(f"不可变历史路径复用不同内容：{path}")
    for kind, by_sequence in sequence_documents.items():
        for sequence, contents in sorted(by_sequence.items()):
            if len(contents) > 1:
                audit.violations.append(f"{kind} 历史序号复用不同内容：{sequence}")
    for label, allocations in (("稳定", sequence_versions), ("刷新", refresh_versions)):
        for sequence, versions in sorted(allocations.items()):
            if len(versions) > 1:
                audit.violations.append(
                    f"不同版本复用历史或已预留{label}序号：{sequence}"
                )
    for path in sorted(set(current_files["github"]) | set(current_files["gitee"])):
        if current_files["github"].get(path) != current_files["gitee"].get(path):
            audit.violations.append(f"双源当前文档字节不一致：{path}")
    return audit
