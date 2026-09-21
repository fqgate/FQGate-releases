"""为公开行情基准提供可复核的直连网络环境。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

_PROXY_VARIABLES = frozenset(
    {
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ftp_proxy",
        "ws_proxy",
        "wss_proxy",
    }
)
_NO_PROXY_VARIABLE = "no_proxy"


@contextmanager
def direct_network_environment() -> Iterator[None]:
    """测试期间清除代理变量，并强制所有地址绕过系统代理。

    requests/urllib 会读取代理环境变量；Windows 上还可能回退到系统代理。
    清除显式代理并设置 ``NO_PROXY=*``，可以让这些库选择直连路径。
    上下文退出时恢复调用方环境，避免基准作为模块运行时污染宿主进程。
    """

    relevant_names = _PROXY_VARIABLES | {_NO_PROXY_VARIABLE}
    original = {
        name: value
        for name, value in os.environ.items()
        if name.lower() in relevant_names
    }
    for name in tuple(os.environ):
        if name.lower() in relevant_names:
            del os.environ[name]

    # 同时设置大小写名称，兼容 POSIX 与 Windows 环境变量处理差异。
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    try:
        yield
    finally:
        for name in tuple(os.environ):
            if name.lower() in relevant_names:
                del os.environ[name]
        os.environ.update(original)
