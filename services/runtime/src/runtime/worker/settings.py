"""Run 队列与 Worker 的配置（S7 §1 / §7）。生效优先级同 runtime.config：环境变量 > 代码默认值。

REDIS_URL 可能含口令，按密钥项处理：不进 repr、不写日志；解析顺序与 runtime.secret_store 一致
（登记为密钥项后走 Vault → ``REDIS_URL_FILE`` → 环境变量；未登记时只支持后两者）。未配置时使用进程内
InMemoryQueue，保持单进程行为（只适用于 RUNTIME_ROLE=all）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from runtime import secret_store

ROLES = ("api", "worker", "all")  # api：只服务 HTTP；worker：只消费 Run 队列；all：两者兼有（单进程开发默认）


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _redis_url() -> str | None:
    """解析 REDIS_URL（含口令时属敏感值）：已登记为密钥项则经 secret_store，否则 ``REDIS_URL_FILE`` → ``REDIS_URL``。"""
    if "REDIS_URL" in secret_store.SECRET_NAMES:
        return secret_store.get("REDIS_URL") or None
    path = os.environ.get("REDIS_URL_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip() or None
        except OSError as e:
            raise secret_store.SecretError(f"REDIS_URL_FILE 指向的文件不可读：{path}") from e
    return os.environ.get("REDIS_URL") or None


def redis_url_source() -> str:
    """REDIS_URL 的实际来源：vault | file | env | default（default 即未配置）。"""
    if "REDIS_URL" in secret_store.SECRET_NAMES:
        return secret_store.source("REDIS_URL")
    if os.environ.get("REDIS_URL_FILE"):
        return "file"
    return "env" if os.environ.get("REDIS_URL") else "default"


@dataclass(frozen=True)
class WorkerSettings:
    """队列 / Worker / 租约的运行参数（秒为单位的均可取小数，便于测试缩短）。"""

    redis_url: str | None = field(default_factory=_redis_url, repr=False)  # 未配置 → InMemoryQueue
    role: str = field(default_factory=lambda: _env("RUNTIME_ROLE", "all"))  # api | worker | all
    concurrency: int = field(default_factory=lambda: int(_env("WORKER_CONCURRENCY", "4")))  # 单进程同时驱动的 Run 上限
    lease_ttl_s: float = field(default_factory=lambda: float(_env("LEASE_TTL_S", "15")))  # 租约有效期（数据库时钟）
    heartbeat_s: float = field(default_factory=lambda: float(_env("HEARTBEAT_S", "5")))  # 续期 + XCLAIM JUSTID 间隔
    claim_idle_s: float = field(default_factory=lambda: float(_env("CLAIM_IDLE_S", "15")))  # XAUTOCLAIM 的 min-idle
    claim_interval_s: float = field(default_factory=lambda: float(_env("CLAIM_INTERVAL_S", "5")))  # XAUTOCLAIM 扫描间隔
    reaper_s: float = field(default_factory=lambda: float(_env("REAPER_S", "10")))  # reaper 扫描间隔
    max_deliveries: int = field(default_factory=lambda: int(_env("MAX_DELIVERIES", "5")))  # 超过即进死信流，Run failed
    # 失租 / 发现取消后先中断 harness，再给 drive 这么久自行收尾，超时即取消 drive 任务
    isolate_grace_s: float = field(default_factory=lambda: float(_env("ISOLATE_GRACE_S", "10")))

    @property
    def runs_worker(self) -> bool:
        """本进程是否消费 Run 队列（并运行 reaper / XAUTOCLAIM）。"""
        return self.role in ("worker", "all")


def problems(ws: WorkerSettings, runtime_env: str) -> list[str]:
    """配置自检：返回不满足的条目（空列表表示通过）。生产（runtime_env=prod）额外要求 Redis 且口令不经明文环境变量注入。"""
    out: list[str] = []
    if ws.role not in ROLES:
        out.append(f"RUNTIME_ROLE 必须为 {'|'.join(ROLES)}，当前为 {ws.role!r}")
    if ws.role in ("api", "worker") and not ws.redis_url:
        out.append(f"RUNTIME_ROLE={ws.role} 需要 REDIS_URL（进程内队列只适用于 RUNTIME_ROLE=all）")
    if ws.concurrency < 1:
        out.append("WORKER_CONCURRENCY 必须 ≥ 1")
    if ws.max_deliveries < 1:
        out.append("MAX_DELIVERIES 必须 ≥ 1")
    if not 0 < ws.heartbeat_s < ws.lease_ttl_s:
        out.append("HEARTBEAT_S 必须大于 0 且小于 LEASE_TTL_S")
    if runtime_env == "prod":
        if not ws.redis_url:
            out.append("生产环境必须配置 REDIS_URL（多节点 Run 队列）")
        elif redis_url_source() == "env":
            out.append("REDIS_URL 来自明文环境变量，生产须经 Vault 或 REDIS_URL_FILE 注入")
    return out


def validate(ws: WorkerSettings, runtime_env: str) -> None:
    """配置不合法时拒绝启动。"""
    found = problems(ws, runtime_env)
    if found:
        raise RuntimeError("Run 队列 / Worker 配置不合法：" + "；".join(found))


worker_settings = WorkerSettings()  # 进程级单例
