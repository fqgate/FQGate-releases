"""只从钉住的远端 Git 对象读取 V2 全历史，不读取本地工作树。

GitHub 的 commits/trees/blobs 合同见其官方 REST Git 文档；Gitee 的同名
接口已用公开仓库只读核对：commit.tree.sha、truncated、base64 blob。
分页始终读到空页，树截断或对象摘要不一致均为硬错误，不能当作首次发布。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from urllib.parse import urlencode


V2_ROOT = "releases/v2"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
PAGE_SIZE = 100


def _sha(value, label: str) -> str:
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise RuntimeError(f"{label} 缺少有效的完整 Git SHA")
    return value


def _commit(value, expected: str | None = None) -> tuple[str, str]:
    if not isinstance(value, dict) or not isinstance(value.get("commit"), dict):
        raise RuntimeError("远端提交对象畸形")
    sha = _sha(value.get("sha"), "远端提交")
    tree = value["commit"].get("tree")
    if not isinstance(tree, dict):
        raise RuntimeError("远端提交缺少精确树对象")
    tree_sha = _sha(tree.get("sha"), "远端提交树")
    if expected is not None and sha != expected:
        raise RuntimeError("远端提交身份与钉住的 SHA 不一致")
    return sha, tree_sha


def _repository_base(api, channel: str) -> str:
    if channel not in {"github", "gitee"}:
        raise ValueError("不支持的 V2 历史来源")
    repository = getattr(api, "repository", None)
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise RuntimeError("历史来源缺少明确的 owner/repository")
    return f"/repos/{repository}"


def _blob(api, base: str, sha: str, expected_size: int) -> bytes:
    value = api.json_request("GET", f"{base}/git/blobs/{sha}")
    if (
        not isinstance(value, dict)
        or value.get("sha") != sha
        or value.get("encoding") != "base64"
        or type(value.get("size")) is not int
        or value["size"] != expected_size
        or not isinstance(value.get("content"), str)
    ):
        raise RuntimeError("远端 blob 身份、大小或编码不符合约定")
    try:
        content = base64.b64decode("".join(value["content"].split()), validate=True)
    except (ValueError, binascii.Error) as error:
        raise RuntimeError("远端 blob 不是完整 Base64 内容") from error
    digest = hashlib.sha1(
        f"blob {len(content)}\0".encode("ascii") + content
    ).hexdigest()
    if len(content) != expected_size or digest != sha:
        raise RuntimeError("远端 blob 内容被截断或 Git SHA 不匹配")
    return content


def _tree_files(api, base: str, tree_sha: str, blob_cache: dict) -> dict[str, bytes]:
    value = api.json_request("GET", f"{base}/git/trees/{tree_sha}?recursive=1")
    if (
        not isinstance(value, dict)
        or value.get("sha") != tree_sha
        or value.get("truncated") is not False
        or not isinstance(value.get("tree"), list)
    ):
        raise RuntimeError("远端树身份错误、结果截断或缺少完整性标记")
    files = {}
    seen = set()
    for entry in value["tree"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RuntimeError("远端树条目畸形")
        path = entry["path"]
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in seen
        ):
            raise RuntimeError("远端树包含重复或不安全路径")
        seen.add(path)
        if path != V2_ROOT and not path.startswith(V2_ROOT + "/"):
            continue
        sha = _sha(entry.get("sha"), f"远端树 {path}")
        if entry.get("type") == "tree":
            if entry.get("mode") not in {"040000", "40000"}:
                raise RuntimeError(f"V2 目录类型无效：{path}")
            continue
        if (
            path == V2_ROOT
            or entry.get("type") != "blob"
            or entry.get("mode") not in {"100644", "100755"}
            or type(entry.get("size")) is not int
            or entry["size"] < 0
        ):
            raise RuntimeError(f"V2 历史只允许普通文件：{path}")
        if sha not in blob_cache:
            blob_cache[sha] = _blob(api, base, sha, entry["size"])
        content = blob_cache[sha]
        if len(content) != entry["size"]:
            raise RuntimeError(f"同一 Git blob 的大小不一致：{path}")
        files[path] = content
    return files


def history_snapshots(
    api, channel: str
) -> tuple[str, list[tuple[str, dict[str, bytes]]]]:
    """API 类的只读委托入口；返回 oldest → 钉住 main HEAD 的文件快照。

    api 只需提供 repository 和 json_request；GitHub/Gitee 路径均使用
    /repos/{owner}/{repo}。所有读取都用固定 SHA，最后再次核对 main 没有变化。
    """
    base = _repository_base(api, channel)
    head, head_tree = _commit(api.json_request("GET", f"{base}/commits/main"))
    commits = []
    seen_commits = set()
    page = 1
    while True:
        query = urlencode(
            {"sha": head, "path": V2_ROOT, "per_page": PAGE_SIZE, "page": page}
        )
        values = api.json_request("GET", f"{base}/commits?{query}")
        if not isinstance(values, list) or len(values) > PAGE_SIZE:
            raise RuntimeError("远端历史分页畸形或超出请求页大小")
        if not values:
            break
        for value in values:
            sha, tree_sha = _commit(value)
            if sha in seen_commits:
                raise RuntimeError("远端历史分页重复，无法确认完整性")
            seen_commits.add(sha)
            commits.append((sha, tree_sha))
        page += 1

    blob_cache = {}
    tree_cache = {}
    snapshots = []
    # commits 接口按新到旧返回；不以日期排序，避免作者时间被改写。
    ordered = list(reversed(commits))
    if head not in seen_commits:
        ordered.append((head, head_tree))
    elif not ordered or ordered[-1][0] != head:
        raise RuntimeError("远端历史顺序与钉住的 main HEAD 不一致")
    for sha, tree_sha in ordered:
        if sha == head and tree_sha != head_tree:
            raise RuntimeError("钉住的提交引用了不同树对象")
        if tree_sha not in tree_cache:
            tree_cache[tree_sha] = _tree_files(api, base, tree_sha, blob_cache)
        snapshots.append((sha, dict(tree_cache[tree_sha])))
    if head not in seen_commits:
        preceding = snapshots[-2][1] if len(snapshots) > 1 else {}
        if snapshots[-1][1] != preceding:
            raise RuntimeError("历史列表遗漏了当前 V2 文件变更，不能确认完整性")
    final_head, final_tree = _commit(api.json_request("GET", f"{base}/commits/main"))
    if (final_head, final_tree) != (head, head_tree):
        raise RuntimeError(f"{channel} main 在历史审计期间发生变化")
    return head, snapshots


def assert_history_anchor(api, channel: str, anchor_sha: str, head_sha: str) -> None:
    """确认冻结基线仍可从钉住的 main HEAD 到达，不能以旧对象存在代替。

    不带 path 过滤，覆盖没有改动 V2 文件的基线提交。按远端提交列表完整
    分页，找到目标后再次核对 main；错误或历史耗尽必须阻断续跑。
    """
    base = _repository_base(api, channel)
    anchor_sha = _sha(anchor_sha, "冻结历史基线")
    head_sha = _sha(head_sha, "钉住的 main HEAD")
    initial_head, initial_tree = _commit(
        api.json_request("GET", f"{base}/commits/main"), expected=head_sha
    )
    seen_commits = set()
    page = 1
    while True:
        query = urlencode({"sha": head_sha, "per_page": PAGE_SIZE, "page": page})
        values = api.json_request("GET", f"{base}/commits?{query}")
        if not isinstance(values, list) or len(values) > PAGE_SIZE:
            raise RuntimeError("远端全历史分页畸形或超出请求页大小")
        if not values:
            raise RuntimeError(f"{channel} 冻结历史基线不再从当前 HEAD 可达")
        if page == 1:
            _commit(values[0], expected=head_sha)
        found = False
        for value in values:
            commit, tree = _commit(value)
            if commit in seen_commits:
                raise RuntimeError("远端全历史分页重复，无法确认基线可达性")
            if commit == head_sha and tree != initial_tree:
                raise RuntimeError("钉住的提交引用了不同树对象")
            seen_commits.add(commit)
            found = found or commit == anchor_sha
        if found:
            final_head, final_tree = _commit(
                api.json_request("GET", f"{base}/commits/main")
            )
            if (final_head, final_tree) != (initial_head, initial_tree):
                raise RuntimeError(f"{channel} main 在基线可达性审计期间发生变化")
            return
        page += 1
