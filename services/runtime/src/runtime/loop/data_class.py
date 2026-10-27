"""Runtime 侧的数据分级判定（S9 OPT-4，D152）：按契约 ④ 的六通道矩阵（``ai_contract.data_class.Matrix``）决定一个 Run 的数据
能否流向某个处理方。

- Run 的数据等级 = 污点集合中最高的等级（``run_level``；污点来自工具结果与附件，见 ``toolbroker.taint_of``），无污点为 public；
- 模型通道：runner 在每轮调用模型前判定 ``allows("model", level, alias)``，替代旧配置 ``CONFIDENTIAL_MODEL_ALIASES``
  （开发态兼容：旧配置中的别名并入 confidential 等级；生产由 ``config.production_problems`` 拒绝旧配置）；
- 回退通道：``fallbacks_allowed`` 为 False 时请求带 ``disable_fallbacks``（等级 ≥ internal 一律关闭）；
- 观测通道：``observe_mode`` 决定 Trace 记原文（public）、脱敏后记录（internal，经 redact_pii）还是只记元数据（confidential 或
  矩阵未准许）；
- 记忆通道：``memory_allowed`` 守卫（平台尚无跨会话记忆实现，架构测试保证新增实现必须经过它）。

矩阵按文件路径缓存；加载失败时抛 ``MatrixError`` / ``OSError``（调用方失败关闭）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from ai_contract.data_class import Level, Matrix
from runtime.config import Settings, settings

OBSERVE_PROCESSOR = "langfuse"  # 平台 Trace 与网关回调的观测后端（OTel Collector → Langfuse）

ObserveMode = Literal["full", "redacted", "metadata"]


@lru_cache(maxsize=8)
def _load(path: str, legacy_confidential: tuple[str, ...], dev: bool) -> Matrix:
    m = Matrix.load(path)
    if not dev or not legacy_confidential:
        return m
    # 开发态兼容旧配置：CONFIDENTIAL_MODEL_ALIASES 中的别名并入 model 通道 confidential 等级
    rules = {ch: dict(levels) for ch, levels in m.rules.items()}
    model = rules.setdefault("model", {})
    model[Level.CONFIDENTIAL.value] = frozenset(model.get(Level.CONFIDENTIAL.value, frozenset())) | frozenset(legacy_confidential)
    return Matrix(rules=rules, version=m.version, signoff=m.signoff)


def matrix(cfg: Settings | None = None) -> Matrix:
    """当前生效的矩阵（DATA_CLASS_MATRIX_FILE，开发态默认随源码的开发矩阵）。cfg 缺省取进程配置（调用方可传入自己模块里的配置）。"""
    c = cfg or settings
    return _load(c.data_class_matrix_file, c.confidential_model_aliases, c.runtime_env != "prod")


def run_level(taint: set[str] | frozenset[str] | list[str]) -> Level:
    """Run 的数据等级：污点中最高的等级；无污点为 public。"""
    return Level.highest(taint)


def model_allowed(alias: str, level: Level, cfg: Settings | None = None) -> bool:
    """该等级的上下文能否发给该模型别名（模型通道）。"""
    return matrix(cfg).allows("model", level, alias)


def fallbacks_allowed(alias: str, level: Level, cfg: Settings | None = None) -> bool:
    """该等级的请求能否由网关自动回退到其他模型（回退通道，processor 为请求的别名）；等级 ≥ internal 一律不允许。"""
    return level < Level.INTERNAL and matrix(cfg).allows("fallback", level, alias)


def observe_mode(level: Level, cfg: Settings | None = None) -> ObserveMode:
    """观测通道：矩阵准许且为 public 时记原文；准许且为 internal 时脱敏后记录；其余只记元数据。"""
    if level >= Level.CONFIDENTIAL or not matrix(cfg).allows("observe", level, OBSERVE_PROCESSOR):
        return "metadata"
    return "full" if level == Level.PUBLIC else "redacted"


def memory_allowed(level: Level, store: str, cfg: Settings | None = None) -> bool:
    """跨会话记忆通道守卫：写入（含会话摘要、附件派生内容）前必须调用；摘要与附件继承来源的等级。"""
    return matrix(cfg).allows("memory", level, store)
