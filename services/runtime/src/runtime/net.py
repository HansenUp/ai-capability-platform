"""统一出站客户端（S9.6，D147）：Runtime 访问网络的唯一 httpx 工厂。

- ``client(**kw)`` 返回 ``httpx.AsyncClient``，其 transport 外包一层出站检查：每个请求（含重定向的每一跳）的目标按
  scheme + host + port **精确匹配**白名单，越界抛 ``EgressDenied``（``httpx.HTTPError`` 的子类：既有"网络错误按失败
  关闭处理"的调用方无需改动即拒绝）。测试注入的 transport 同样被包裹，出站检查在测试中照样生效。
- 白名单 = 由现有配置推导的平台内部地址（模型网关、能力网关、策略服务、观测适配器、Langfuse、OTel 导出端点、Keycloak
  JWKS / 令牌端点、Console、节点令牌端点）∪ ``EGRESS_ALLOWLIST``（逗号分隔，只取每项的 scheme://host:port，路径忽略）。
  不做前缀匹配：``http://localhost:8082`` 不放行 ``http://localhost:8082.evil.com``、``http://localhost:80821`` 或
  ``http://localhost:8082@evil.com``。
- ``audit_sources(paths)``：AST 扫描本模块之外构造 ``httpx.AsyncClient(`` / ``httpx.Client(`` 的位置（CI 检查用，WP-D 接入）。
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from runtime.config import settings

EGRESS_DENIED = "EGRESS_DENIED"
_DEFAULT_PORTS = {"http": 80, "https": 443}
Origin = tuple[str, str, int]


class EgressDenied(httpx.HTTPError):
    """出站目标不在白名单内（code=EGRESS_DENIED）。"""

    code = EGRESS_DENIED

    def __init__(self, url: str) -> None:
        super().__init__(f"egress denied: {origin_text(url) or url!r} is not in the egress allowlist")
        self.url = url


def origin(url: str | httpx.URL) -> Origin | None:
    """URL → (scheme, host, port)；scheme 只接受 http / https，缺端口按默认端口补齐；无法解析返回 None。"""
    try:
        u = httpx.URL(str(url))
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    scheme = u.scheme.lower()
    host = (u.host or "").lower().rstrip(".")
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    return scheme, host, u.port or _DEFAULT_PORTS[scheme]


def origin_text(url: str | httpx.URL) -> str | None:
    o = origin(url)
    return f"{o[0]}://{o[1]}:{o[2]}" if o else None


def _configured_urls() -> list[str]:
    """由现有配置推导的平台内部出站目标（未配置的项跳过）。"""
    from runtime.node import settings as node_settings  # 延迟导入：node.settings 只读环境变量，但避免导入期循环

    ns = node_settings.load()
    urls: list[str | None] = [
        settings.model_gateway_url,
        settings.capability_gateway_url,
        settings.policy_url,
        settings.observe_adapter_url,
        settings.langfuse_host,
        settings.otel_exporter_endpoint,
        settings.jwks_url,
        settings.gateway_token_url,
        ns.console_url,
        ns.token_url,
    ]
    return [u for u in urls if u]


def allowed_origins() -> frozenset[Origin]:
    """当前白名单（每次调用按配置重算：配置是进程级冻结对象，开销可忽略，测试可直接替换配置）。"""
    extra = [x.strip() for x in os.environ.get("EGRESS_ALLOWLIST", "").split(",") if x.strip()]
    out: set[Origin] = set()
    for u in [*_configured_urls(), *settings.egress_allowlist, *extra]:
        o = origin(u)
        if o is not None:
            out.add(o)
    return frozenset(out)


def allowed(url: str | httpx.URL) -> bool:
    """目标是否在白名单内（scheme + host + port 精确匹配）。"""
    o = origin(url)
    return o is not None and o in allowed_origins()


def check(url: str | httpx.URL) -> None:
    """越界抛 EgressDenied。"""
    if not allowed(url):
        raise EgressDenied(str(url))


class _EgressTransport(httpx.AsyncBaseTransport):
    """在真实 transport 之前做出站检查（重定向的每一跳都经过这里）。"""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        check(request.url)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def client(**kw: Any) -> httpx.AsyncClient:
    """唯一出站工厂：参数同 ``httpx.AsyncClient``；``transport`` 为 None 时使用默认网络 transport。

    ``base_url`` 若给出也须在白名单内（构造即检查，尽早失败）。不支持 ``mounts`` / ``proxy``（会绕过出站检查）。"""
    if "mounts" in kw or "proxy" in kw or "proxies" in kw:
        raise ValueError("net.client does not accept mounts / proxy (would bypass the egress check)")
    base_url = kw.get("base_url")
    if base_url:
        check(str(base_url))
    inner = kw.pop("transport", None) or httpx.AsyncHTTPTransport()
    kw.setdefault("trust_env", False)  # 不读 HTTP(S)_PROXY 等环境变量：出站路径只由白名单决定
    return httpx.AsyncClient(transport=_EgressTransport(inner), **kw)


# ---- CI 检查（WP-D 接入 scripts / CI）----
_CTOR_NAMES = {"AsyncClient", "Client"}


def _violations(tree: ast.AST) -> list[tuple[int, str]]:
    """找出 httpx.AsyncClient( / httpx.Client( 以及 from httpx import AsyncClient/Client 后直接构造的位置。"""
    httpx_aliases = {"httpx"}
    direct: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "httpx":
                    httpx_aliases.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
            for a in node.names:
                if a.name in _CTOR_NAMES:
                    direct[a.asname or a.name] = a.name
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in _CTOR_NAMES and isinstance(f.value, ast.Name) and f.value.id in httpx_aliases:
            out.append((node.lineno, f"httpx.{f.attr}("))
        elif isinstance(f, ast.Name) and f.id in direct:
            out.append((node.lineno, f"httpx.{direct[f.id]}("))
    return sorted(out)


def audit_sources(paths: Iterable[str | Path]) -> list[str]:
    """扫描给定文件 / 目录下的 .py（以 python 读取明文，兼容本机透明加密），返回 ``路径:行号: 构造`` 列表；
    本模块（runtime/net.py）自身除外。空列表表示通过。"""
    me = Path(__file__).resolve()
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(x for x in p.rglob("*.py") if "__pycache__" not in x.parts)
        elif p.suffix == ".py":
            files.append(p)
    findings: list[str] = []
    for f in files:
        if f.resolve() == me:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except (OSError, SyntaxError, UnicodeDecodeError) as e:
            findings.append(f"{f}:0: unreadable ({type(e).__name__})")
            continue
        findings += [f"{f}:{line}: {what} outside runtime.net" for line, what in _violations(tree)]
    return findings


__all__ = ["EGRESS_DENIED", "EgressDenied", "allowed", "allowed_origins", "audit_sources", "check", "client", "origin"]
