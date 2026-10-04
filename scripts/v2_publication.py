"""V2 单写者发布日志：分配、冻结、向前补齐、独立验收。

两个托管平台没有跨仓库事务。仓库中的签名记录才是续跑依据；工作流
artifact 仅供诊断。不得把静态安全状态模拟或外部报告校验称为运行验收。
"""

from __future__ import annotations

import re
from dataclasses import replace

import finalize_v2_release as protocol
from v2_history import collect_history
from v2_transition import transition_report


PUBLICATION_ROOT = f"{protocol.V2_RELEASE_ROOT}/publications"
IN_FLIGHT_PATH = f"{PUBLICATION_ROOT}/in-flight.json"
DIGEST = re.compile(r"^[0-9a-f]{64}$")
COMMON_FIELDS = {"schemaVersion", "kind", "version", "sequence", "refreshSequence"}
RESERVATION_FIELDS = COMMON_FIELDS | {
    "candidateSha256",
    "historyHeads",
    "previousFreshness",
    "previousReservationSha256",
}
INTENT_FIELDS = COMMON_FIELDS | {
    "candidateSha256",
    "reservationSha256",
    "publishedAt",
    "documents",
    "transition",
}
COMPLETION_FIELDS = COMMON_FIELDS | {
    "candidateSha256",
    "intentSha256",
    "acceptanceSha256",
}


def publication_path(version: str, kind: str) -> str:
    return f"{PUBLICATION_ROOT}/{version}/{kind}.json"


def candidate_digest(candidate: dict) -> str:
    # draft/publishedAt 是发行状态，不是资产身份；公开后不得改变续跑身份。
    identity = {
        key: value
        for key, value in candidate.items()
        if key not in {"draft", "publishedAt"}
    }
    return protocol.sha256_bytes(protocol.canonical_json(identity))


def require_candidate_snapshot(github, version, digest, contents):
    observed, observed_contents = protocol.collect_candidate(
        github, github.repository, version
    )
    if candidate_digest(observed) != digest or observed_contents != contents:
        raise RuntimeError("发布期间 GitHub 候选资产已变化，拒绝公开或切换入口")
    return observed


def read_verified_public_mirror(gitee, candidate, contents):
    release = gitee.release_by_tag(candidate["tag"])
    if release is None or release.get("prerelease") is not False:
        raise RuntimeError("Gitee 待验收 Release 不存在或尚未公开")
    mirrored = {}
    for asset in gitee.assets(release):
        name = protocol.asset_name(asset)
        if name in mirrored or name not in contents:
            raise RuntimeError("Gitee 验收资产集合重复或出现额外文件")
        if gitee.download_asset(release, asset) != contents[name]:
            raise RuntimeError(f"Gitee 验收资产字节不一致：{name}")
        mirrored[name] = asset
    if set(mirrored) != set(contents):
        raise RuntimeError("Gitee 验收资产集合不完整")
    return mirrored


def verify_publication_assets(
    github,
    gitee,
    version,
    reservation,
    contents,
    intent,
    release_public_key,
    freshness_public_key,
):
    candidate = require_candidate_snapshot(
        github, version, reservation["candidateSha256"], contents
    )
    if (
        candidate["draft"]
        or protocol.parse_github_timestamp(candidate["publishedAt"])
        != intent["publishedAt"]
    ):
        raise RuntimeError("GitHub 公开状态或 published_at 不符合验收 intent")
    mirrored = read_verified_public_mirror(gitee, candidate, contents)
    validate_intent_documents(
        intent, candidate, mirrored, release_public_key, freshness_public_key
    )
    return candidate


def positive_integer(value) -> bool:
    return type(value) is int and 0 < value < 2**64


def validate_management(path: str, value: dict) -> None:
    kind = value.get("kind")
    fields = {
        "reservation": RESERVATION_FIELDS,
        "intent": INTENT_FIELDS,
        "completion": COMPLETION_FIELDS,
    }
    if (
        kind not in fields
        or set(value) != fields[kind]
        or type(value["schemaVersion"]) is not int
        or value["schemaVersion"] != 1
    ):
        raise ValueError(f"发布日志字段无效：{path}")
    protocol.validate_v2_version(value["version"])
    if path != publication_path(value["version"], kind) and not (
        kind == "reservation" and path == IN_FLIGHT_PATH
    ):
        raise ValueError(f"发布日志路径与身份不符：{path}")
    if not positive_integer(value["sequence"]) or not positive_integer(
        value["refreshSequence"]
    ):
        raise ValueError(f"发布日志序号无效：{path}")
    for key in fields[kind]:
        if key.endswith("Sha256"):
            if key == "previousReservationSha256" and value[key] is None:
                continue
            if not isinstance(value[key], str) or not DIGEST.fullmatch(value[key]):
                raise ValueError(f"发布日志摘要无效：{key}")
    if not isinstance(value["candidateSha256"], str) or not DIGEST.fullmatch(
        value["candidateSha256"]
    ):
        raise ValueError("发布候选摘要无效")
    if kind == "reservation":
        heads = value["historyHeads"]
        if (
            not isinstance(heads, dict)
            or set(heads) != {"github", "gitee"}
            or any(
                not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)
                for head in heads.values()
            )
        ):
            raise ValueError("发布日志历史 HEAD 无效")
        if value["previousFreshness"] is not None:
            protocol.base64url_decode(value["previousFreshness"], "原 freshness")
    elif kind == "intent":
        if not positive_integer(value["publishedAt"]):
            raise ValueError("发布日志公开时间无效")
        expected = {
            f"{protocol.V2_RELEASE_ROOT}/{value['version']}/release.json",
            f"{protocol.V2_RELEASE_ROOT}/{value['sequence']}/stable.json",
            protocol.ROOT_FRESHNESS_PATH,
        }
        if (
            not isinstance(value["documents"], dict)
            or set(value["documents"]) != expected
        ):
            raise ValueError("发布 intent 必须冻结完整三份签名文档")
        for encoded in value["documents"].values():
            protocol.base64url_decode(encoded, "冻结签名文档")
        if (
            not isinstance(value["transition"], dict)
            or value["transition"].get("allowed") is not True
        ):
            raise ValueError("发布 intent 缺少通过的静态状态预检")
        if not isinstance(value["transition"].get("stateIds"), list):
            raise ValueError("发布 intent 缺少历史状态覆盖清单")
    else:
        if not isinstance(value["acceptanceSha256"], str) or not DIGEST.fullmatch(
            value["acceptanceSha256"]
        ):
            raise ValueError("完成记录缺少运行验收报告摘要")


def signed_management(path: str, document: bytes, public_key: str) -> dict:
    value = protocol.verify_document(
        document, public_key, protocol.RELEASE_SIGNING_KEY_ID
    )
    validate_management(path, value)
    return value


def audit_history(github, gitee, release_public_key, freshness_public_key):
    return collect_history(
        github,
        gitee,
        release_public_key,
        freshness_public_key,
        management_validator=validate_management,
    )


def history_report(history) -> dict:
    return {
        "heads": history.heads,
        "maxSequence": history.max_sequence,
        "maxRefreshSequence": history.max_refresh_sequence,
        "violations": history.violations,
        "snapshots": len(history.snapshots),
    }


def current_documents(history) -> dict[str, bytes]:
    latest = {}
    for snapshot in history.snapshots:
        latest[snapshot.channel] = snapshot.files
    merged = {}
    for files in latest.values():
        for path, content in files.items():
            if path.endswith("/release.json"):
                if path in merged and merged[path] != content:
                    raise RuntimeError(f"两端当前 release 内容不一致：{path}")
                merged[path] = content
    return merged


def require_clean_history(history) -> None:
    if history.violations:
        raise RuntimeError(
            "发行历史存在删除、改写、回退或断链；必须先批准独立的向前恢复方案："
            + "; ".join(history.violations)
        )


def require_pinned_heads(github, gitee, history) -> None:
    for name, channel in (("github", github), ("gitee", gitee)):
        if channel.current_head() != history.heads[name]:
            raise RuntimeError(f"{name} main 在历史审计后发生变化，必须重新审计")


def require_reservation_anchors(github, gitee, history, reservation) -> None:
    for name, channel in (("github", github), ("gitee", gitee)):
        channel.history_anchor(reservation["historyHeads"][name], history.heads[name])


def allow_logged_partial_writes(
    history,
    github,
    gitee,
    reservation,
    reservation_bytes,
    previous_lock,
    public_key,
    freshness_public_key,
):
    """只豁免可由本次已签名日志解释的当前半写，不豁免任何历史污染。"""
    if reservation is None:
        return history
    allowed = {
        IN_FLIGHT_PATH: (previous_lock, reservation_bytes),
        publication_path(reservation["version"], "reservation"): (
            None,
            reservation_bytes,
        ),
    }
    intent_path = publication_path(reservation["version"], "intent")
    intent_bytes = unique_document(github, gitee, intent_path, history)
    if intent_bytes is not None:
        intent = signed_management(intent_path, intent_bytes, public_key)
        if intent["reservationSha256"] != protocol.sha256_bytes(
            reservation_bytes
        ) or any(
            intent[key] != reservation[key]
            for key in ("version", "sequence", "refreshSequence", "candidateSha256")
        ):
            raise RuntimeError("半写 intent 与 reservation 身份不一致")
        allowed[intent_path] = (None, intent_bytes)
        documents = {
            path: protocol.base64url_decode(raw, path)
            for path, raw in intent["documents"].items()
        }
        # 不依赖管理签名推断客户端签名；完整验签并核对链摘要。
        fresh = protocol.verify_document(
            documents[protocol.ROOT_FRESHNESS_PATH],
            freshness_public_key,
            protocol.FRESHNESS_SIGNING_KEY_ID,
        )
        protocol.validate_freshness_payload(fresh)
        stable_path = f"{protocol.V2_RELEASE_ROOT}/{fresh['stablePath']}"
        if (
            stable_path not in documents
            or fresh["refreshSequence"] != reservation["refreshSequence"]
            or protocol.sha256_bytes(documents[stable_path]) != fresh["stableSha256"]
        ):
            raise RuntimeError("半写 intent freshness 摘要无效")
        stable = protocol.verify_document(
            documents[stable_path], public_key, protocol.RELEASE_SIGNING_KEY_ID
        )
        protocol.validate_stable_payload(stable)
        release_path = f"{protocol.V2_RELEASE_ROOT}/{stable['releasePath']}"
        if (
            release_path not in documents
            or stable["sequence"] != reservation["sequence"]
            or stable["version"] != reservation["version"]
            or protocol.sha256_bytes(documents[release_path]) != stable["releaseSha256"]
        ):
            raise RuntimeError("半写 intent stable 摘要无效")
        release = protocol.verify_document(
            documents[release_path], public_key, protocol.RELEASE_SIGNING_KEY_ID
        )
        protocol.validate_release_payload(release)
        if (
            release["sequence"] != reservation["sequence"]
            or release["version"] != reservation["version"]
        ):
            raise RuntimeError("半写 intent release 身份无效")
        for path, raw in documents.items():
            if path != protocol.ROOT_FRESHNESS_PATH:
                allowed[path] = (None, raw)
        previous = (
            None
            if reservation["previousFreshness"] is None
            else protocol.base64url_decode(
                reservation["previousFreshness"], "原 freshness"
            )
        )
        allowed[protocol.ROOT_FRESHNESS_PATH] = (
            previous,
            documents[protocol.ROOT_FRESHNESS_PATH],
        )
        completion_path = publication_path(reservation["version"], "completion")
        completion_bytes = unique_document(github, gitee, completion_path, history)
        if completion_bytes is not None:
            completion = signed_management(
                completion_path, completion_bytes, public_key
            )
            if completion["intentSha256"] != protocol.sha256_bytes(intent_bytes) or any(
                completion[key] != reservation[key]
                for key in ("version", "sequence", "refreshSequence", "candidateSha256")
            ):
                raise RuntimeError("半写 completion 身份不一致")
            allowed[completion_path] = (None, completion_bytes)
    permitted = set()
    for path, values in allowed.items():
        if all(channel.read_content(path) in values for channel in (github, gitee)):
            permitted.add(f"双源当前文档字节不一致：{path}")
    return replace(
        history,
        violations=[issue for issue in history.violations if issue not in permitted],
    )


def unique_document(github, gitee, path: str, history=None) -> bytes | None:
    observed = {
        value
        for value in (github.read_content(path), gitee.read_content(path))
        if value is not None
    }
    if history is not None:
        observed.update(history.documents.get(path, set()))
    if len(observed) > 1:
        raise RuntimeError(f"发布记录存在不同字节，拒绝猜测：{path}")
    return next(iter(observed), None)


def freeze_documents(
    candidate,
    mirrored,
    sequence,
    refresh_sequence,
    published_at,
    release_key,
    freshness_key,
) -> dict[str, bytes]:
    release_payload = protocol.build_release_payload(
        candidate, mirrored, sequence, published_at
    )
    protocol.validate_release_payload(release_payload)
    release_bytes = protocol.sign_document(
        release_payload, release_key, protocol.RELEASE_SIGNING_KEY_ID
    )
    stable_payload = {
        "schemaVersion": 1,
        "channel": "stable",
        "sequence": sequence,
        "version": release_payload["version"],
        "releasePath": f"{release_payload['version']}/release.json",
        "releaseSha256": protocol.sha256_bytes(release_bytes),
    }
    stable_bytes = protocol.sign_document(
        stable_payload, release_key, protocol.RELEASE_SIGNING_KEY_ID
    )
    freshness_payload = {
        "schemaVersion": 1,
        "channel": "stable",
        "refreshSequence": refresh_sequence,
        "stablePath": f"{sequence}/stable.json",
        "stableSha256": protocol.sha256_bytes(stable_bytes),
    }
    return {
        f"{protocol.V2_RELEASE_ROOT}/{release_payload['version']}/release.json": release_bytes,
        f"{protocol.V2_RELEASE_ROOT}/{sequence}/stable.json": stable_bytes,
        protocol.ROOT_FRESHNESS_PATH: protocol.sign_document(
            freshness_payload, freshness_key, protocol.FRESHNESS_SIGNING_KEY_ID
        ),
    }


def validate_intent_documents(
    intent, candidate, mirrored, release_public_key, freshness_public_key
) -> dict[str, bytes]:
    documents = {
        path: protocol.base64url_decode(value, path)
        for path, value in intent["documents"].items()
    }
    release_path = f"{protocol.V2_RELEASE_ROOT}/{intent['version']}/release.json"
    stable_path = f"{protocol.V2_RELEASE_ROOT}/{intent['sequence']}/stable.json"
    release = protocol.verify_document(
        documents[release_path], release_public_key, protocol.RELEASE_SIGNING_KEY_ID
    )
    protocol.validate_release_payload(release)
    if release != protocol.build_release_payload(
        candidate, mirrored, intent["sequence"], intent["publishedAt"]
    ):
        raise RuntimeError("intent 冻结的 release 与当前候选/镜像不一致，拒绝重签")
    stable = protocol.verify_document(
        documents[stable_path], release_public_key, protocol.RELEASE_SIGNING_KEY_ID
    )
    protocol.validate_stable_payload(stable)
    if stable != {
        "schemaVersion": 1,
        "channel": "stable",
        "sequence": intent["sequence"],
        "version": intent["version"],
        "releasePath": f"{intent['version']}/release.json",
        "releaseSha256": protocol.sha256_bytes(documents[release_path]),
    }:
        raise RuntimeError("intent stable 身份或摘要不匹配")
    freshness = protocol.verify_document(
        documents[protocol.ROOT_FRESHNESS_PATH],
        freshness_public_key,
        protocol.FRESHNESS_SIGNING_KEY_ID,
    )
    protocol.validate_freshness_payload(freshness)
    if freshness != {
        "schemaVersion": 1,
        "channel": "stable",
        "refreshSequence": intent["refreshSequence"],
        "stablePath": f"{intent['sequence']}/stable.json",
        "stableSha256": protocol.sha256_bytes(documents[stable_path]),
    }:
        raise RuntimeError("intent freshness 身份或摘要不匹配")
    return documents


def check_history_identity(history, reservation, documents=None) -> None:
    if (
        reservation["sequence"] < history.max_sequence
        or reservation["refreshSequence"] < history.max_refresh_sequence
    ):
        raise RuntimeError("已存在更高的历史序号，拒绝继续旧发布")
    version = reservation["version"]
    for path in history.documents:
        match = re.fullmatch(r"releases/v2/(\d+\.\d+\.\d+)/release.json", path)
        if (
            match
            and match[1] != version
            and protocol.parse_version(match[1]) >= protocol.parse_version(version)
        ):
            raise RuntimeError("已存在不低于候选版本的历史发布，拒绝回退版本")
    if documents is not None:
        for path, content in documents.items():
            if path != protocol.ROOT_FRESHNESS_PATH and any(
                previous != content for previous in history.documents.get(path, set())
            ):
                raise RuntimeError(f"目标不可变路径曾使用不同字节：{path}")


def require_transition(history, reservation, documents, *, reserved: bool) -> dict:
    report = transition_report(
        history,
        reservation["version"],
        reservation["sequence"],
        reservation["refreshSequence"],
        {**current_documents(history), **documents},
        allow_reserved_sequence=reserved,
    )
    if report.get("allowed") is not True:
        raise RuntimeError("旧客户端安全状态预检失败；禁止自动发布或清除用户状态")
    return report


def reservation_for_new(history, candidate, active, previous_lock):
    version = candidate["tag"].removeprefix("fqgate-v")
    if active and protocol.parse_version(version) <= protocol.parse_version(
        active["stable"]["version"]
    ):
        raise ValueError("新稳定版本必须高于当前版本；历史版本只能按同一 intent 续跑")
    if active and (
        active["stable"]["sequence"] > history.max_sequence
        or active["freshness"]["refreshSequence"] > history.max_refresh_sequence
        or active["freshnessBytes"]
        not in history.documents.get(protocol.ROOT_FRESHNESS_PATH, set())
    ):
        raise RuntimeError("当前稳定入口未被钉住的历史覆盖，必须重新审计")
    if history.documents.get(f"{protocol.V2_RELEASE_ROOT}/{version}/release.json"):
        raise RuntimeError("版本已有历史 release；没有匹配的签名发布日志，禁止复用")
    return {
        "schemaVersion": 1,
        "kind": "reservation",
        "version": version,
        "sequence": history.max_sequence + 1,
        "refreshSequence": history.max_refresh_sequence + 1,
        "candidateSha256": candidate_digest(candidate),
        "historyHeads": history.heads,
        "previousFreshness": (
            None
            if active is None
            else protocol.base64url_encode(active["freshnessBytes"])
        ),
        "previousReservationSha256": (
            None if previous_lock is None else protocol.sha256_bytes(previous_lock)
        ),
    }


def claim_reservation(
    github, gitee, reservation, document, expected_lock, history, public_key
) -> None:
    # GitHub 是唯一分配权威；Gitee 不提供跨平台锁。所有入口必须单写者运行。
    # 某端超时/半写保留记录，不能以删除锁或换版本的方式跳过。
    require_pinned_heads(github, gitee, history)
    require_reservation_anchors(github, gitee, history, reservation)
    allowed_locks = (expected_lock, document)
    for channel in (github, gitee):
        if channel.read_content(IN_FLIGHT_PATH) not in allowed_locks:
            raise RuntimeError("存在其他在途发布，拒绝抢占")
    protocol.checked_write(
        github, IN_FLIGHT_PATH, document, "保留 FQGate V2 发布序号", expected_lock
    )
    protocol.checked_write(
        gitee, IN_FLIGHT_PATH, document, "镜像 FQGate V2 发布序号", expected_lock
    )
    path = publication_path(reservation["version"], "reservation")
    if any(old != document for old in history.documents.get(path, set())):
        raise RuntimeError("历史 reservation 不一致")
    protocol.deploy_immutable(github, gitee, path, document, "记录 FQGate V2 发布分配")
    signed_management(path, document, public_key)


def load_reservation(github, gitee, version, history, candidate, public_key):
    path = publication_path(version, "reservation")
    document = unique_document(github, gitee, path, history)
    locks = [channel.read_content(IN_FLIGHT_PATH) for channel in (github, gitee)]
    for lock in locks:
        if lock is not None:
            value = signed_management(IN_FLIGHT_PATH, lock, public_key)
            if value["version"] == version:
                if document is not None and document != lock:
                    raise RuntimeError("in-flight 与版本 reservation 不一致")
                document = lock
    if document is None:
        previous = protocol.read_identical((github, gitee), IN_FLIGHT_PATH)
        if previous is not None:
            old = signed_management(IN_FLIGHT_PATH, previous, public_key)
            completion_path = publication_path(old["version"], "completion")
            completed = protocol.read_identical((github, gitee), completion_path)
            if completed is None:
                raise RuntimeError(
                    f"{old['version']} 尚未完成运行验收，禁止启动另一发布"
                )
            completion = signed_management(completion_path, completed, public_key)
            prior_reservation = protocol.read_identical(
                (github, gitee), publication_path(old["version"], "reservation")
            )
            old_intent_path = publication_path(old["version"], "intent")
            old_intent_bytes = protocol.read_identical((github, gitee), old_intent_path)
            if prior_reservation != previous or old_intent_bytes is None:
                raise RuntimeError("完成记录缺少双端 reservation → intent 来源链")
            old_intent = signed_management(
                old_intent_path, old_intent_bytes, public_key
            )
            keys = ("version", "sequence", "refreshSequence", "candidateSha256")
            if (
                any(
                    completion[key] != old[key] or old_intent[key] != old[key]
                    for key in keys
                )
                or old_intent["reservationSha256"] != protocol.sha256_bytes(previous)
                or completion["intentSha256"] != protocol.sha256_bytes(old_intent_bytes)
            ):
                raise RuntimeError("完成记录与在途发布身份不一致")
            for old_path, encoded in old_intent["documents"].items():
                if protocol.read_identical(
                    (github, gitee), old_path
                ) != protocol.base64url_decode(encoded, old_path):
                    raise RuntimeError("已完成发布的线上签名链不匹配，禁止分配下一版")
        return None, None, previous
    reservation = signed_management(path, document, public_key)
    if reservation["candidateSha256"] != candidate_digest(candidate):
        raise RuntimeError("同一版本的候选身份已变化，禁止重签或覆盖")
    previous = None
    previous_digest = reservation["previousReservationSha256"]
    if previous_digest is not None:
        previous = next(
            (
                raw
                for raw in history.documents.get(IN_FLIGHT_PATH, set())
                if protocol.sha256_bytes(raw) == previous_digest
            ),
            None,
        )
        if previous is None:
            raise RuntimeError("reservation 指向的前一锁记录无法核验")
    if any(lock not in (previous, document) for lock in locks):
        raise RuntimeError("另一发布已占用序号分配入口，拒绝继续旧版本")
    return reservation, document, previous


def preview_mirrored(candidate):
    return {
        package["package"]["fileName"]: {
            "name": package["package"]["fileName"],
            "browser_download_url": f"https://gitee.com/preview/{package['package']['fileName']}",
        }
        for package in candidate["packages"]
    }


def inspect_publication(
    *, github, gitee, version, release_public_key, freshness_public_key
) -> dict:
    candidate, _ = protocol.collect_candidate(github, github.repository, version)
    history = audit_history(github, gitee, release_public_key, freshness_public_key)
    raw_audit = history_report(history)
    reservation, reservation_bytes, previous_lock = load_reservation(
        github, gitee, version, history, candidate, release_public_key
    )
    history = allow_logged_partial_writes(
        history,
        github,
        gitee,
        reservation,
        reservation_bytes,
        previous_lock,
        release_public_key,
        freshness_public_key,
    )
    result = {
        "status": "blocked" if history.violations else "audited",
        "version": version,
        "candidateSha256": candidate_digest(candidate),
        "history": history_report(history),
    }
    result["rawAudit"] = raw_audit
    if history.violations:
        result["requiresApprovedForwardRecovery"] = True
        return result
    if reservation is not None:
        result.update(
            status="resume_existing_reservation",
            sequence=reservation["sequence"],
            refreshSequence=reservation["refreshSequence"],
        )
        return result
    active = protocol.read_active_state(
        github, gitee, release_public_key, freshness_public_key
    )
    proposal = reservation_for_new(history, candidate, active, None)
    check_history_identity(history, proposal)
    result.update(
        sequence=proposal["sequence"], refreshSequence=proposal["refreshSequence"]
    )
    result["limitations"] = [
        "只读审计与序号建议；未上传、公开、签名或运行客户端",
        "正式发布仍需完整静态状态预检和切换后运行验收",
    ]
    return result


def finalize_publication(
    *,
    github,
    gitee,
    version,
    release_private_key,
    freshness_private_key,
    release_public_key,
    freshness_public_key,
) -> dict:
    candidate, contents = protocol.collect_candidate(github, github.repository, version)
    history = audit_history(github, gitee, release_public_key, freshness_public_key)
    reservation, reservation_bytes, expected_lock = load_reservation(
        github, gitee, version, history, candidate, release_public_key
    )
    history = allow_logged_partial_writes(
        history,
        github,
        gitee,
        reservation,
        reservation_bytes,
        expected_lock,
        release_public_key,
        freshness_public_key,
    )
    require_clean_history(history)
    if reservation is None:
        active = protocol.read_active_state(
            github, gitee, release_public_key, freshness_public_key
        )
        reservation = reservation_for_new(history, candidate, active, expected_lock)
        check_history_identity(history, reservation)
        preview = freeze_documents(
            candidate,
            preview_mirrored(candidate),
            reservation["sequence"],
            reservation["refreshSequence"],
            1_700_000_000,
            release_private_key,
            freshness_private_key,
        )
        require_transition(history, reservation, preview, reserved=False)
        reservation_bytes = protocol.sign_document(
            reservation, release_private_key, protocol.RELEASE_SIGNING_KEY_ID
        )
    else:
        check_history_identity(history, reservation)
    claim_reservation(
        github,
        gitee,
        reservation,
        reservation_bytes,
        expected_lock,
        history,
        release_public_key,
    )
    gitee_release, mirrored = protocol.mirror_to_gitee(gitee, candidate, contents)
    intent_path = publication_path(version, "intent")
    intent_bytes = unique_document(github, gitee, intent_path, history)
    if intent_bytes is None:
        # 静态预检必须在公开前通过。公开时间只能来自真实 GitHub Release。
        preview_time = (
            1_700_000_000
            if candidate["publishedAt"] is None
            else protocol.parse_github_timestamp(candidate["publishedAt"])
        )
        preview = freeze_documents(
            candidate,
            mirrored,
            reservation["sequence"],
            reservation["refreshSequence"],
            preview_time,
            release_private_key,
            freshness_private_key,
        )
        require_transition(history, reservation, preview, reserved=True)
        require_candidate_snapshot(
            github, version, reservation["candidateSha256"], contents
        )
        github_release, _ = protocol.publish_release_pair(
            github, gitee, candidate, gitee_release
        )
        published_at = protocol.parse_github_timestamp(github_release["published_at"])
        documents = freeze_documents(
            candidate,
            mirrored,
            reservation["sequence"],
            reservation["refreshSequence"],
            published_at,
            release_private_key,
            freshness_private_key,
        )
        report = require_transition(history, reservation, documents, reserved=True)
        intent = {
            **{key: reservation[key] for key in COMMON_FIELDS},
            "kind": "intent",
            "candidateSha256": candidate_digest(candidate),
            "reservationSha256": protocol.sha256_bytes(reservation_bytes),
            "publishedAt": published_at,
            "documents": {
                path: protocol.base64url_encode(raw) for path, raw in documents.items()
            },
            "transition": report,
        }
        validate_management(intent_path, intent)
        intent_bytes = protocol.sign_document(
            intent, release_private_key, protocol.RELEASE_SIGNING_KEY_ID
        )
    else:
        intent = signed_management(intent_path, intent_bytes, release_public_key)
        if intent["reservationSha256"] != protocol.sha256_bytes(
            reservation_bytes
        ) or intent["candidateSha256"] != candidate_digest(candidate):
            raise RuntimeError("intent 不属于当前 reservation/candidate")
        if (
            candidate["draft"]
            or gitee_release.get("prerelease", True)
            or protocol.parse_github_timestamp(candidate["publishedAt"])
            != intent["publishedAt"]
        ):
            raise RuntimeError("公开发行状态或 published_at 与冻结 intent 不一致")
        documents = validate_intent_documents(
            intent, candidate, mirrored, release_public_key, freshness_public_key
        )
        require_transition(history, reservation, documents, reserved=True)
    check_history_identity(history, reservation, documents)
    validate_intent_documents(
        intent, candidate, mirrored, release_public_key, freshness_public_key
    )
    verify_publication_assets(
        github,
        gitee,
        version,
        reservation,
        contents,
        intent,
        release_public_key,
        freshness_public_key,
    )
    protocol.deploy_immutable(
        github, gitee, intent_path, intent_bytes, "冻结 FQGate V2 发布内容"
    )
    previous = (
        None
        if reservation["previousFreshness"] is None
        else protocol.base64url_decode(reservation["previousFreshness"], "原 freshness")
    )
    if any(
        channel.read_content(protocol.ROOT_FRESHNESS_PATH)
        not in (previous, documents[protocol.ROOT_FRESHNESS_PATH])
        for channel in (github, gitee)
    ):
        raise RuntimeError("freshness 不属于 reservation 前态或目标态，拒绝覆盖")
    # intent JSON 的键按 canonical 排序，不能将字典顺序当作依赖顺序。
    for path in (
        f"{protocol.V2_RELEASE_ROOT}/{version}/release.json",
        f"{protocol.V2_RELEASE_ROOT}/{reservation['sequence']}/stable.json",
    ):
        protocol.deploy_immutable(
            github, gitee, path, documents[path], "补齐 FQGate V2 不可变签名链"
        )
    # 自己的写入已改变 HEAD；重新审计后才允许切换，不能沿用旧历史上限。
    before_switch = audit_history(
        github, gitee, release_public_key, freshness_public_key
    )
    before_switch = allow_logged_partial_writes(
        before_switch,
        github,
        gitee,
        reservation,
        reservation_bytes,
        expected_lock,
        release_public_key,
        freshness_public_key,
    )
    require_clean_history(before_switch)
    require_reservation_anchors(github, gitee, before_switch, reservation)
    check_history_identity(before_switch, reservation, documents)
    require_transition(before_switch, reservation, documents, reserved=True)
    verify_publication_assets(
        github,
        gitee,
        version,
        reservation,
        contents,
        intent,
        release_public_key,
        freshness_public_key,
    )
    require_pinned_heads(github, gitee, before_switch)
    # 此时两源目标链均已回读；只有 root freshness 允许从固定前态向目标前进。
    protocol.switch_freshness(
        github,
        gitee,
        documents[protocol.ROOT_FRESHNESS_PATH],
        previous,
        "前进 FQGate V2 稳定入口",
    )
    protocol.read_active_state(github, gitee, release_public_key, freshness_public_key)
    completion_path = publication_path(version, "completion")
    completion_bytes = protocol.read_identical((github, gitee), completion_path)
    completed = False
    if completion_bytes is not None:
        completion = signed_management(
            completion_path, completion_bytes, release_public_key
        )
        if completion["intentSha256"] != protocol.sha256_bytes(intent_bytes):
            raise RuntimeError("完成记录不属于当前 intent")
        completed = True
    return {
        "status": (
            "published"
            if completed
            else "metadata_activated_pending_runtime_acceptance"
        ),
        "version": version,
        "sequence": reservation["sequence"],
        "refreshSequence": reservation["refreshSequence"],
        "publishedAt": intent["publishedAt"],
        "candidateSha256": candidate_digest(candidate),
        "intentSha256": protocol.sha256_bytes(intent_bytes),
        "stateIds": intent["transition"]["stateIds"],
        "history": history_report(history),
        "runtimeAcceptanceRequired": not completed,
    }


def accept_publication(
    *,
    github,
    gitee,
    version,
    report,
    release_private_key,
    release_public_key,
    freshness_public_key,
) -> dict:
    from v2_acceptance import validate_acceptance

    candidate, contents = protocol.collect_candidate(github, github.repository, version)
    history = audit_history(github, gitee, release_public_key, freshness_public_key)
    intent_path = publication_path(version, "intent")
    intent_bytes = protocol.read_identical((github, gitee), intent_path)
    if intent_bytes is None:
        raise RuntimeError("缺少已双端落地的签名 intent")
    intent = signed_management(intent_path, intent_bytes, release_public_key)
    reservation, reservation_bytes, previous_lock = load_reservation(
        github, gitee, version, history, candidate, release_public_key
    )
    history = allow_logged_partial_writes(
        history,
        github,
        gitee,
        reservation,
        reservation_bytes,
        previous_lock,
        release_public_key,
        freshness_public_key,
    )
    require_clean_history(history)
    if reservation is None or intent["reservationSha256"] != protocol.sha256_bytes(
        reservation_bytes
    ):
        raise RuntimeError("验收 intent 与在途 reservation 不匹配")
    verify_publication_assets(
        github,
        gitee,
        version,
        reservation,
        contents,
        intent,
        release_public_key,
        freshness_public_key,
    )
    check_history_identity(history, reservation)
    active = protocol.read_active_state(
        github, gitee, release_public_key, freshness_public_key
    )
    if active is None or active["stable"]["version"] != version:
        raise RuntimeError("待验收版本并非双端当前稳定版")
    for path, value in intent["documents"].items():
        if protocol.read_identical((github, gitee), path) != protocol.base64url_decode(
            value, path
        ):
            raise RuntimeError("验收期间线上签名链已变化")
    require_reservation_anchors(github, gitee, history, reservation)
    require_pinned_heads(github, gitee, history)
    validate_acceptance(
        report,
        candidate_digest(candidate),
        version,
        protocol.sha256_bytes(intent_bytes),
        intent["sequence"],
        intent["refreshSequence"],
        intent["transition"]["stateIds"],
    )
    completion = {
        **{key: intent[key] for key in COMMON_FIELDS},
        "kind": "completion",
        "candidateSha256": candidate_digest(candidate),
        "intentSha256": protocol.sha256_bytes(intent_bytes),
        "acceptanceSha256": protocol.sha256_bytes(protocol.canonical_json(report)),
    }
    path = publication_path(version, "completion")
    validate_management(path, completion)
    completion_bytes = protocol.sign_document(
        completion, release_private_key, protocol.RELEASE_SIGNING_KEY_ID
    )
    # 响应失败后只能提交相同报告；不能修改已被记录的验收。
    if any(old != completion_bytes for old in history.documents.get(path, set())):
        raise RuntimeError("验收报告与历史完成记录不一致")
    protocol.deploy_immutable(
        github, gitee, path, completion_bytes, "记录 FQGate V2 运行验收完成"
    )
    return {
        "status": "published",
        "version": version,
        "sequence": intent["sequence"],
        "refreshSequence": intent["refreshSequence"],
        "acceptanceSha256": completion["acceptanceSha256"],
        "acceptanceSource": "operator_or_controlled_runner_report",
    }
