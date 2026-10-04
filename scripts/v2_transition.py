"""已验签 V2 历史链的静态安全状态迁移模拟；不执行发布或客户端操作。

输入历史来自 v2_history 的完整验签审计，候选文档必须由发布入口先验签。
此模块只对照 Rust UpdateGovernor 的反回放及当前版本摘要锁定规则，不把
签名解包视为验签，也不模拟上游时间、包安装、GUI 或客户端进程存活。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from v2_history import HistoryAudit


ROOT = "releases/v2"
FRESHNESS_PATH = f"{ROOT}/freshness.json"
RELEASE_KEY = "update-release-signing-v1"
FRESHNESS_KEY = "update-freshness-signing-v1"
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
RELEASE_PATH = re.compile(rf"^{ROOT}/({VERSION})/release\.json$")
LIMITATIONS = [
    "只模拟已验签文档的反回放与当前发行摘要锁定，签名信任由调用者提供。",
    "序号严格大于所有历史上限，覆盖不同来源组合缓存的序号基线。",
    "不验证上游 HTTP 时间、二进制打包后有效期、下载、安装及平台 GUI。",
    "不能替代 Rust 客户端策略测试、双源上线回读和各平台真实启动验收。",
    "报告不授权人工安装，不删除用户安全状态，也不关闭任何客户端校验。",
]


def _version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(VERSION, value):
        raise ValueError("静态迁移模拟要求严格三段语义版本")
    return tuple(int(part) for part in value.split("."))


def _sequence(value: int, label: str) -> int:
    if type(value) is not int or not 0 < value <= 2**64 - 1:
        raise ValueError(f"{label} 必须是非零 u64 序号")
    return value


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _payload(document: bytes, key_id: str) -> dict:
    """只解包调用者已验签的文档，禁止把本函数当作密码学验签入口。"""
    try:
        envelope = json.loads(document.decode("utf-8"))
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"keyId", "signed", "signature"}
            or envelope["keyId"] != key_id
        ):
            raise ValueError("已验签文档信封或密钥类型无效")
        encoded = envelope["signed"]
        if not isinstance(encoded, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
            raise ValueError("已验签文档正文编码无效")
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        payload = json.loads(raw.decode("utf-8"))
    except (AttributeError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("静态迁移输入不是已验签签名文档") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != 1
        or payload.get("channel") != "stable"
    ):
        raise ValueError("静态迁移输入的升级正文合同无效")
    return payload


@dataclass(frozen=True)
class _Bundle:
    stable: dict
    freshness: dict
    latest_release: dict
    stable_sha256: str
    freshness_sha256: str
    latest_release_sha256: str


def _bundle(files: dict[str, bytes]) -> _Bundle:
    freshness_bytes = files[FRESHNESS_PATH]
    freshness = _payload(freshness_bytes, FRESHNESS_KEY)
    refresh_sequence = _sequence(freshness["refreshSequence"], "refreshSequence")
    stable_path = freshness["stablePath"]
    if not isinstance(stable_path, str) or not re.fullmatch(
        r"[1-9][0-9]*/stable\.json", stable_path
    ):
        raise ValueError("freshness 的 stablePath 无效")
    stable_bytes = files[f"{ROOT}/{stable_path}"]
    stable = _payload(stable_bytes, RELEASE_KEY)
    sequence = _sequence(stable["sequence"], "stable sequence")
    _version(stable["version"])
    if (
        stable_path != f"{sequence}/stable.json"
        or stable["releasePath"] != f"{stable['version']}/release.json"
    ):
        raise ValueError("升级链路径与序号或版本不一致")
    release_bytes = files[f"{ROOT}/{stable['releasePath']}"]
    release = _payload(release_bytes, RELEASE_KEY)
    if (
        release["version"] != stable["version"]
        or release["sequence"] != sequence
        or freshness["stableSha256"] != _sha256(stable_bytes)
        or stable["releaseSha256"] != _sha256(release_bytes)
    ):
        raise ValueError("升级链路径、摘要或版本绑定不一致")
    # 上面的访问同时保证 refreshSequence 可被 Rust 的 u64 合同接受。
    assert refresh_sequence > 0
    return _Bundle(
        stable,
        freshness,
        release,
        _sha256(stable_bytes),
        _sha256(freshness_bytes),
        _sha256(release_bytes),
    )


def _state(
    bundle: _Bundle, installed_version: str, release_bytes: bytes, source: str
) -> dict:
    current = _payload(release_bytes, RELEASE_KEY)
    if current["version"] != installed_version:
        raise ValueError("历史当前版本文档与安装版本不一致")
    current_sha = _sha256(release_bytes)
    return {
        "schemaVersion": 1,
        "installedVersion": installed_version,
        # 发行链不能证明客户端当时看到的 HTTP Date 与内嵌有效期，不能捏造。
        "trustedUnixSeconds": 0,
        "acceptedStable": {
            "sequence": bundle.stable["sequence"],
            "stableSha256": bundle.stable_sha256,
            "latestReleaseSha256": bundle.latest_release_sha256,
            "version": bundle.stable["version"],
        },
        "currentRelease": {
            "version": installed_version,
            "releaseSha256": current_sha,
            "expiresAt": 0,
        },
        "sources": {
            source: {
                "stableSequence": bundle.stable["sequence"],
                "stableSha256": bundle.stable_sha256,
                "latestReleaseSha256": bundle.latest_release_sha256,
                "currentReleaseSha256": current_sha,
                "freshnessSequence": bundle.freshness["refreshSequence"],
                "freshnessSha256": bundle.freshness_sha256,
            }
        },
    }


def derive_security_states(history: HistoryAudit) -> list[dict]:
    """完整历史链×可读安装版本产生唯一基线，并保留全部来源提交引用。"""
    states = {}
    provenance_seen = {}
    for snapshot in history.snapshots:
        try:
            bundle = _bundle(snapshot.files)
        except (KeyError, ValueError):
            continue
        for path, document in sorted(snapshot.files.items()):
            match = RELEASE_PATH.fullmatch(path)
            if not match:
                continue
            installed_version = match.group(1)
            state = _state(bundle, installed_version, document, snapshot.channel)
            identifier = _sha256(
                json.dumps(
                    state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            )
            if identifier not in states:
                states[identifier] = {
                    "id": identifier,
                    "stateId": identifier,
                    "securityState": state,
                    "unknownRuntimeFields": [
                        "trustedUnixSeconds",
                        "currentRelease.expiresAt",
                    ],
                    "provenance": [],
                }
                provenance_seen[identifier] = set()
            # 管理文档提交可能不改变客户端基线；只评价一次，不删除历史证据。
            provenance = (snapshot.channel, snapshot.commit)
            if provenance not in provenance_seen[identifier]:
                states[identifier]["provenance"].append(
                    {
                        "channel": snapshot.channel,
                        "commit": snapshot.commit,
                    }
                )
                provenance_seen[identifier].add(provenance)
    return list(states.values())


def bind_installed_version(state: dict, installed_version: str) -> dict:
    """与 policy.rs::bind_installed_version 对照，仅清当前发行摘要基线。"""
    _version(installed_version)
    bound = copy.deepcopy(state)
    if bound["installedVersion"] != installed_version:
        bound["installedVersion"] = installed_version
        bound["currentRelease"] = None
        for source in bound["sources"].values():
            source["currentReleaseSha256"] = None
    return bound


def _evaluate(state: dict, bundle: _Bundle, files: dict[str, bytes]) -> list[str]:
    """对照 is_source_replay、accepted_replays；缺当前文档也阻断启动检查。"""
    installed_version = state["installedVersion"]
    current_bytes = files.get(f"{ROOT}/{installed_version}/release.json")
    if current_bytes is None:
        return ["current_release_missing"]
    current = _payload(current_bytes, RELEASE_KEY)
    if current["version"] != installed_version:
        return ["current_release_version_mismatch"]
    current_sha = _sha256(current_bytes)
    errors = []
    for source_id, previous in sorted(state["sources"].items()):
        if bundle.stable["sequence"] < previous["stableSequence"]:
            errors.append(f"source_stable_replay:{source_id}")
        elif bundle.stable["sequence"] == previous["stableSequence"] and (
            bundle.stable_sha256 != previous["stableSha256"]
            or bundle.latest_release_sha256 != previous["latestReleaseSha256"]
            or (
                previous.get("currentReleaseSha256") is not None
                and current_sha != previous["currentReleaseSha256"]
            )
        ):
            errors.append(f"source_stable_equivocation:{source_id}")
        if bundle.freshness["refreshSequence"] < previous["freshnessSequence"]:
            errors.append(f"source_freshness_replay:{source_id}")
        elif (
            bundle.freshness["refreshSequence"] == previous["freshnessSequence"]
            and bundle.freshness_sha256 != previous["freshnessSha256"]
        ):
            errors.append(f"source_freshness_equivocation:{source_id}")
    accepted = state["acceptedStable"]
    if accepted is not None:
        if bundle.stable["sequence"] < accepted["sequence"]:
            errors.append("accepted_stable_replay")
        elif bundle.stable["sequence"] == accepted["sequence"] and (
            bundle.stable_sha256 != accepted["stableSha256"]
            or bundle.latest_release_sha256 != accepted["latestReleaseSha256"]
        ):
            errors.append("accepted_stable_equivocation")
    release = state["currentRelease"]
    if release is not None and (
        current["version"] != release["version"]
        or current_sha != release["releaseSha256"]
    ):
        errors.append("accepted_current_release_conflict")
    return errors


def _hash_conflicts(history: HistoryAudit) -> list[dict]:
    conflicts = []
    for path, documents in sorted(history.documents.items()):
        match = RELEASE_PATH.fullmatch(path)
        if match and len(documents) > 1:
            conflicts.append(
                {
                    "path": path,
                    "version": match.group(1),
                    "sha256": sorted(_sha256(document) for document in documents),
                }
            )
    return conflicts


def _validate_recovery_plan(plan: dict, version: str, heads: dict[str, str]) -> None:
    if not isinstance(plan, dict) or set(plan) != {
        "schemaVersion",
        "mode",
        "targetVersion",
        "historyHeads",
        "approved",
    }:
        raise ValueError("恢复 plan 必须使用独立审批的精确字段合同")
    if (
        type(plan["schemaVersion"]) is not int
        or plan["schemaVersion"] != 1
        or plan["mode"] != "manual-install-preserve-security-state"
        or plan["targetVersion"] != version
        or plan["approved"] is not True
        or not isinstance(plan["historyHeads"], dict)
        or plan["historyHeads"] != heads
    ):
        raise ValueError("恢复 plan 未批准或不匹配目标版本、已审计历史头")


def transition_report(
    history: HistoryAudit,
    version: str,
    sequence: int,
    refresh_sequence: int,
    current_documents: dict[str, bytes],
    *,
    recovery_plan: dict | None = None,
    allow_reserved_sequence: bool = False,
) -> dict:
    """返回逐状态静态审计，不把增加序号当作当前发行摘要冲突的解决方案。"""
    _version(version)
    if type(allow_reserved_sequence) is not bool:
        raise ValueError("预留序号模式必须显式使用布尔值")
    _sequence(sequence, "candidate sequence")
    _sequence(refresh_sequence, "candidate refreshSequence")
    candidate = _bundle(current_documents)
    if (
        candidate.stable["version"],
        candidate.stable["sequence"],
        candidate.freshness["refreshSequence"],
    ) != (version, sequence, refresh_sequence):
        raise ValueError("候选签名链与迁移预检参数不一致")
    states = derive_security_states(history)
    failures = []
    if sequence < history.max_sequence or (
        sequence == history.max_sequence and not allow_reserved_sequence
    ):
        failures.append("sequence_not_above_all_history")
    if refresh_sequence < history.max_refresh_sequence or (
        refresh_sequence == history.max_refresh_sequence and not allow_reserved_sequence
    ):
        failures.append("refresh_sequence_not_above_all_history")
    # 续跑归属由发布入口以已签名 intent 精确校验。只排除与本候选逐字节
    # 相同的自有生产文档，不能把其他旧残留或管理文档伪造为客户端状态。
    owned_paths = {
        FRESHNESS_PATH,
        f"{ROOT}/{sequence}/stable.json",
        f"{ROOT}/{version}/release.json",
    }
    historical_production = {
        path: documents
        for path, documents in history.documents.items()
        if path == FRESHNESS_PATH
        or RELEASE_PATH.fullmatch(path)
        or re.fullmatch(rf"{ROOT}/[1-9][0-9]*/stable\.json", path)
    }
    if allow_reserved_sequence:
        historical_production = {
            path: documents
            for path, documents in historical_production.items()
            if path not in owned_paths
            or any(document != current_documents.get(path) for document in documents)
        }
    if historical_production and not states:
        failures.append("no_complete_verified_historical_chain")
    old_versions = {
        _payload(document, RELEASE_KEY)["version"]
        for path, documents in history.documents.items()
        if RELEASE_PATH.fullmatch(path)
        for document in documents
    }
    own_release_documents = history.documents.get(
        f"{ROOT}/{version}/release.json", set()
    )
    if (
        allow_reserved_sequence
        and own_release_documents
        and all(
            document == current_documents[f"{ROOT}/{version}/release.json"]
            for document in own_release_documents
        )
    ):
        old_versions.discard(version)
    if old_versions and _version(version) <= max(_version(old) for old in old_versions):
        failures.append("version_not_above_all_history")
    automatic = []
    for historical in states:
        reasons = _evaluate(historical["securityState"], candidate, current_documents)
        automatic.append({**historical, "allowed": not reasons, "reasons": reasons})
    conflicts = _hash_conflicts(history)
    requires_manual = bool(conflicts) or any(
        any(
            reason in {"accepted_current_release_conflict", "current_release_missing"}
            for reason in result["reasons"]
        )
        for result in automatic
    )
    automatic_allowed = (
        not failures
        and not history.violations
        and all(result["allowed"] for result in automatic)
    )
    # 审批只允许模拟已明确安装新版本后的状态；它不能覆盖任何反回放失败。
    recovery = []
    recovery_validated = False
    if recovery_plan is not None:
        _validate_recovery_plan(recovery_plan, version, history.heads)
        for historical in states:
            before = historical["securityState"]
            bound = bind_installed_version(before, version)
            reasons = _evaluate(bound, candidate, current_documents)
            if before["installedVersion"] == version:
                reasons.append("installed_version_unchanged")
            recovery.append(
                {
                    "id": historical["id"],
                    "stateId": historical["stateId"],
                    "provenance": copy.deepcopy(historical["provenance"]),
                    "installedVersionBefore": before["installedVersion"],
                    "securityState": bound,
                    "allowed": not reasons,
                    "reasons": reasons,
                    "unknownRuntimeFields": [
                        "trustedUnixSeconds",
                        "installedBinaryExpiresAt",
                    ],
                }
            )
        recovery_validated = (
            not failures
            and bool(states)
            and all(result["allowed"] for result in recovery)
        )
    return {
        "schemaVersion": 1,
        "simulation": "static_update_security_transition",
        "historyHeads": dict(history.heads),
        "candidate": {
            "version": version,
            "sequence": sequence,
            "refreshSequence": refresh_sequence,
        },
        "reservedSequenceMode": allow_reserved_sequence,
        "historyMaxima": {
            "sequence": history.max_sequence,
            "refreshSequence": history.max_refresh_sequence,
        },
        "historyViolations": list(history.violations),
        "preconditionFailures": failures,
        "historicalStateCount": len(states),
        "historicalObservationCount": sum(len(state["provenance"]) for state in states),
        "stateIds": sorted({state["stateId"] for state in states}),
        "perStateAutomatic": automatic,
        "automaticAllowed": automatic_allowed,
        "currentReleaseHashConflicts": conflicts,
        "requires_manual_upgrade": requires_manual,
        "perStateAfterVersionChange": recovery,
        "recoveryValidated": recovery_validated,
        "allowed": automatic_allowed or recovery_validated,
        "limitations": LIMITATIONS.copy(),
    }
