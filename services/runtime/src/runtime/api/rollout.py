"""哈希灰度选版（S3.2）。

一个 Agent 同时最多有两个在用版本：
- 稳定版：最新的已发布且全量（rollout_percent=100）的版本；
- 灰度版：比稳定版更新、已发布且 rollout_percent<100 的最新版本。

未指定版本的请求按主体选版：在灰度版白名单中 → 灰度版；否则按 ``sha256(agent_id:subject)`` 分桶（0～99），
桶号小于 rollout_percent → 灰度版，其余 → 稳定版。同一主体对同一 Agent 的分桶固定（会话粘性），
放量比例只增不减时，已进入灰度的主体不会被换回稳定版。没有稳定版（首个版本即以灰度发布）时：比例大于 0 则全部走
灰度版（only）；比例为 0 则只有白名单可用，其他主体视为没有可用版本（用于"只给白名单试用"的首发）。

只有同租户最新的已发布版本能处于灰度（管理接口保证），因此这里的"灰度版比稳定版新"总成立。

一键回滚（``/admin/.../rollback``）把目标版本置为全量稳定版，其他已发布版本置为 retired，灰度随之结束。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Literal

from runtime.state.db import AgentVersion

Reason = Literal["pinned", "stable", "canary", "allowlist", "only"]


def bucket(agent_id: str, subject: str) -> int:
    """主体在该 Agent 上的灰度桶号（0～99），对同一 (agent_id, subject) 恒定。"""
    digest = hashlib.sha256(f"{agent_id}:{subject}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def choose(published: Sequence[AgentVersion], subject: str) -> tuple[AgentVersion | None, Reason]:
    """从某 Agent 的已发布版本中为主体选版；published 不要求有序，按 id 新旧判断。"""
    ordered = sorted(published, key=lambda v: v.id, reverse=True)
    stable = next((v for v in ordered if v.rollout_percent >= 100), None)
    canary = next((v for v in ordered if v.rollout_percent < 100 and (stable is None or v.id > stable.id)), None)
    if canary is None:
        return stable, "stable"
    if subject in (canary.rollout_allowlist or []):
        return canary, "allowlist"
    if stable is None:
        return (canary, "only") if canary.rollout_percent > 0 else (None, "only")
    if bucket(canary.agent_id, subject) < canary.rollout_percent:
        return canary, "canary"
    return stable, "stable"
