"""dsh-pet 本机 HTTP 端点：只碰回环、绕过代理、失败即静默。

桌宠独立运行时监听 127.0.0.1，写操作要带运行时令牌（`standalone-runtime.json` 里那份）。
这里只负责把请求送到、把 JSON 解回来，业务语义（档位、退出）一律以仓库的 openapi.yaml 为准。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

#: 桌宠路由前缀，与 dsh-pet/src/shared/constants.ts 的 ROUTE_SEGMENT 保持一致。
ROUTE_SEGMENT = "/dsh-pet-7340"

#: 显式空 ProxyHandler：回环请求绝不能被 HTTP_PROXY / 代理软件接走。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 1.5,
    with_route: bool = True,
) -> tuple[int, Any]:
    """发一个本机请求。

    :param port: 运行时实际端口。
    :param path: 路由内的相对路径（`/state`），或 `with_route=False` 时的运维路径（`/health`）。
    :param token: 运行时令牌；只读 GET 可省略。
    :returns: ``(状态码, 解析后的 JSON 或 None)``。
    :raises OSError: 连不上、超时等传输层失败，由调用方决定是否吞掉。
    """
    url = f"http://127.0.0.1:{port}{ROUTE_SEGMENT if with_route else ''}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("x-dsh-pet-token", token)
    with _OPENER.open(req, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, (json.loads(body) if body else None)