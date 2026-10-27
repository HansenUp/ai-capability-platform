"""远程拉取与动态执行规则（remote_fetch.*）：硬规则"禁止运行时远程拉取可执行内容"。

- remote_fetch.command：文本文件中出现下载 / 安装命令（curl、wget、pip install、git clone、npm install、Invoke-WebRequest 等）；
- remote_fetch.url：任何文件中出现不在 SKILL_URL_ALLOWLIST（主机精确匹配）的 URL；
- remote_fetch.network_import：脚本导入网络模块（requests、urllib、socket、httpx、http.client、aiohttp 等）；
- remote_fetch.dynamic_exec：脚本使用进程 / 动态执行（subprocess、os.system / popen / exec* / spawn*、eval、exec、compile、
  __import__、importlib 等）；
- remote_fetch.unparsable：脚本无法解析（失败关闭）。
"""

from __future__ import annotations

import ast
import re
from urllib.parse import urlsplit

from capability_gateway import config
from capability_gateway.scan.model import Finding, SkillPackage, finding, line_of

RULES = {
    "remote_fetch.command": "文本中出现下载 / 安装命令",
    "remote_fetch.url": "出现非白名单 URL",
    "remote_fetch.network_import": "脚本导入网络模块",
    "remote_fetch.dynamic_exec": "脚本使用进程或动态执行",
    "remote_fetch.unparsable": "脚本无法解析",
}

COMMANDS = re.compile(
    r"(?i)\b(curl|wget|aria2c|pip3?\s+install|python3?\s+-m\s+pip\s+install|uv\s+pip\s+install|git\s+clone|npm\s+(install|i)\b"
    r"|npx\s|pnpm\s+(add|install)|yarn\s+add|apt(-get)?\s+install|brew\s+install|invoke-webrequest|iwr\s|go\s+get)"
)
URL = re.compile(r"(?i)\b(?:https?|ftp|wss?)://[^\s<>\"'`)\]]+")
NET_MODULES = frozenset(
    {
        "requests",
        "urllib",
        "urllib2",
        "urllib3",
        "http",
        "httpx",
        "aiohttp",
        "socket",
        "ssl",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "xmlrpc",
        "websocket",
        "websockets",
        "paramiko",
        "pycurl",
        "asyncio",  # 含 open_connection 等网络原语；沙箱脚本不需要
    }
)
EXEC_MODULES = frozenset(
    {"subprocess", "pty", "ctypes", "multiprocessing", "importlib", "runpy", "code", "marshal", "pickle", "shutil", "builtins"}
)
EXEC_BUILTINS = frozenset({"eval", "exec", "compile", "__import__", "breakpoint", "__builtins__", "globals", "vars"})
OS_EXEC = re.compile(r"^(system|popen|exec\w*|spawn\w*|fork\w*|posix_spawn\w*|kill\w*)$")


def _url_findings(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    for m in URL.finditer(text):
        host = (urlsplit(m.group(0)).hostname or "").lower()
        if host not in config.SKILL_URL_ALLOWLIST:
            out.append(finding("remote_fetch.url", path, f"URL host '{host}' is not allowlisted", line=line_of(text, m.start())))
    return out


def _script_findings(path: str, src: str) -> list[Finding]:
    try:
        tree = ast.parse(src, filename=path)
    except (SyntaxError, ValueError) as e:
        return [finding("remote_fetch.unparsable", path, f"cannot parse script: {type(e).__name__}", line=getattr(e, "lineno", None))]
    out: list[Finding] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module or ""]
            if (node.module or "") == "os":
                mods += [f"os.{a.name}" for a in node.names if OS_EXEC.match(a.name)]
        for mod in mods:
            root = mod.split(".", 1)[0]
            if root in NET_MODULES:
                out.append(finding("remote_fetch.network_import", path, f"network module '{mod}' is not allowed", line=line))
            elif root in EXEC_MODULES or mod.startswith("os."):
                out.append(
                    finding("remote_fetch.dynamic_exec", path, f"process / dynamic execution module '{mod}' is not allowed", line=line)
                )
        if isinstance(node, ast.Name) and node.id in EXEC_BUILTINS:
            out.append(finding("remote_fetch.dynamic_exec", path, f"'{node.id}' is not allowed", line=line))
        if isinstance(node, ast.Attribute):
            if OS_EXEC.match(node.attr):  # 不限定 os.：import os as x 后 x.system 同样拒绝
                out.append(finding("remote_fetch.dynamic_exec", path, f"'.{node.attr}' is not allowed", line=line))
            if node.attr in ("__builtins__", "__subclasses__", "__globals__", "__code__"):
                out.append(finding("remote_fetch.dynamic_exec", path, f"'{node.attr}' introspection is not allowed", line=line))
    return out


def check(pkg: SkillPackage) -> list[Finding]:
    out: list[Finding] = []
    for path, text in sorted(pkg.texts().items()):
        out += _url_findings(path, text)
        if path.endswith(".py"):
            out += _script_findings(path, text)
            continue
        for m in COMMANDS.finditer(text):
            out.append(
                finding("remote_fetch.command", path, f"download / install command '{m.group(0).strip()}'", line=line_of(text, m.start()))
            )
    return out
