"""校验操作员或受控运行器提供的 V2 实机验收报告合同。

本模块不启动客户端，也不读取、审查 evidenceSha256 对应的原始日志。
合同校验通过只表示报告完整、声明成功且绑定本次发布，不表示本脚本
亲自执行或独立证实了实机验收。调用方必须显式取得真实验收报告。
"""

from __future__ import annotations

import re
from collections.abc import Iterable


TARGETS = frozenset({("windows", "x86_64"), ("macos", "aarch64"), ("macos", "x86_64")})
REPORT_FIELDS = frozenset(
    {
        "schemaVersion",
        "version",
        "candidateSha256",
        "intentSha256",
        "sequence",
        "refreshSequence",
        "cases",
    }
)
CASE_FIELDS = frozenset(
    {
        "platform",
        "architecture",
        "stateId",
        "processRunning",
        "coreRunning",
        "updateCheckCompleted",
        "automaticExit",
        "securityStatePreserved",
        "evidenceSha256",
    }
)
SUCCESS_FIELDS = (
    "processRunning",
    "coreRunning",
    "updateCheckCompleted",
    "securityStatePreserved",
)
SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")


def _digest(value, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{label} 必须是完整的 SHA-256 十六进制摘要")


def _positive_integer(value, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} 必须是正整数，不能使用布尔值")


def _expected_cases(state_ids: Iterable[str]) -> set[tuple[str, str, str]]:
    if isinstance(state_ids, (str, bytes, dict)):
        raise ValueError("历史安全状态必须是唯一字符串 ID 的集合")
    try:
        iterator = iter(state_ids)
    except TypeError as error:
        raise ValueError("历史安全状态必须是唯一字符串 ID 的集合") from error
    states = {"fresh"}
    for state_id in iterator:
        if (
            not isinstance(state_id, str)
            or not state_id
            or state_id != state_id.strip()
            or any(ord(value) < 32 or ord(value) == 127 for value in state_id)
        ):
            raise ValueError("历史安全状态 ID 必须是非空且无控制字符的字符串")
        if state_id in states:
            raise ValueError("历史安全状态 ID 重复或占用了保留的 fresh 状态")
        states.add(state_id)
    return {
        (platform, architecture, state_id)
        for platform, architecture in TARGETS
        for state_id in states
    }


def validate_acceptance(
    report: dict,
    candidate_sha256: str,
    version: str,
    intent_sha256: str,
    sequence: int,
    refresh_sequence: int,
    state_ids: Iterable[str],
) -> dict:
    """严格校验真实报告声明，原样返回；不证明原始证据或代替实机运行。

    state_ids 是待覆盖的历史安全状态 ID，不包含自动加入的 fresh。
    调用方读取 JSON 时还必须拒绝重复键；重复键信息在 dict 中已丢失。
    """
    _digest(candidate_sha256, "已批准候选摘要")
    _digest(intent_sha256, "已签名发布意图摘要")
    _positive_integer(sequence, "已签名稳定序号")
    _positive_integer(refresh_sequence, "已签名入口序号")
    if not isinstance(version, str) or not version:
        raise ValueError("待验收版本不能为空")
    expected_cases = _expected_cases(state_ids)
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        raise ValueError("验收报告字段不符合约定")
    if type(report["schemaVersion"]) is not int or report["schemaVersion"] != 1:
        raise ValueError("验收报告 schemaVersion 必须为整数 1")
    _digest(report["candidateSha256"], "验收报告候选摘要")
    _digest(report["intentSha256"], "验收报告发布意图摘要")
    _positive_integer(report["sequence"], "验收报告稳定序号")
    _positive_integer(report["refreshSequence"], "验收报告入口序号")
    expected_identity = {
        "version": version,
        "candidateSha256": candidate_sha256,
        "intentSha256": intent_sha256,
        "sequence": sequence,
        "refreshSequence": refresh_sequence,
    }
    if any(report[field] != value for field, value in expected_identity.items()):
        raise ValueError("验收报告与已签名发布意图、候选或版本不一致")
    if not isinstance(report["cases"], list):
        raise ValueError("验收报告 cases 必须是数组")
    if len(report["cases"]) != len(expected_cases):
        raise ValueError("验收报告没有完整覆盖要求的平台和安全状态矩阵")
    seen = set()
    for case in report["cases"]:
        if not isinstance(case, dict) or set(case) != CASE_FIELDS:
            raise ValueError("验收案例字段不符合约定")
        identity_fields = ("platform", "architecture", "stateId")
        if any(not isinstance(case[field], str) for field in identity_fields):
            raise ValueError("验收案例的平台、架构和安全状态必须是字符串")
        identity = tuple(case[field] for field in identity_fields)
        if identity not in expected_cases:
            raise ValueError("验收案例包含要求范围外的平台、架构或安全状态")
        if identity in seen:
            raise ValueError("验收案例重复，不能替代缺失的平台或安全状态")
        seen.add(identity)
        if any(case[field] is not True for field in SUCCESS_FIELDS):
            raise ValueError("验收案例没有明确证明进程、Core、更新校验及安全状态成功")
        if case["automaticExit"] is not False:
            raise ValueError("验收案例存在自动退出或未明确确认没有自动退出")
        _digest(case["evidenceSha256"], "验收案例原始日志摘要")
    if seen != expected_cases:
        raise ValueError("验收报告没有完整覆盖要求的平台和安全状态矩阵")
    return report
