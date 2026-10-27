"""Hooks 引擎（S5 WP-A，D72～D76）：平台在 pre_model / post_model / pre_tool / post_tool 四个点统一执行 hook。

- 只允许平台内置命名 hook（D73）：Agent 版本快照 `config.hooks=[{point, name, params}]`，按声明顺序成链，
  首个 deny 即停；发布时由 `validate_hooks_config` 校验（名称未知、点不匹配或参数非法 → 发布 409）。
- 失败关闭（D74）：纯函数 hook 超时 1s，调模型的 hook 超时 15s；出错 / 超时时 pre_* 按 deny 处理，
  post_tool 扣留输出（调用方据 deny 处理），post_model 只记录、不覆盖（thinking_summary 失败即不下发思维链）。
- 覆盖键白名单（HOOK_OVERRIDABLE）：hook 返回值中白名单以外的键忽略并记日志；post_tool 的 data_class 只升不降。
- 重放一致（D75）：本模块只负责执行并给出结论与记录（records，只含名称 / 结论 / 元数据，不含原文）；
  结论由 runner / 工具代理写进受影响的事件，重放时读事件，不重跑 hook。
- 适配器内部点：`HookChain.hookset(points)` 把同一条链包装成契约的 HookFn，只含适配器在
  `HarnessCapabilities.internal_hook_points` 声明的点。
"""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ai_contract import HOOK_OVERRIDABLE, HookPoint, HookSet
from ai_contract.harness import HookFn
from runtime import llm
from runtime.config import settings

log = logging.getLogger(__name__)

PURE_TIMEOUT_S = 1.0  # 纯函数 hook 超时（秒）
MODEL_TIMEOUT_S = 15.0  # 调模型的 hook 超时（秒）

# 数据分级的高低次序：post_tool 的 data_class 覆盖只允许升级；未知取值按最高级处理（失败关闭）。
DATA_CLASS_RANK = {"public": 0, "internal": 1, "confidential": 2}

# 结论取值：allow 放行未改写；override 放行并改写了白名单内的键；deny hook 主动拒绝；error / timeout 执行失败
VERDICTS = ("allow", "override", "deny", "error", "timeout")


class HookConfigError(ValueError):
    """Agent 版本快照中的 hooks 配置不合规（发布时 409，运行期失败关闭）。"""


HookImpl = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any] | None]]  # (params, payload) → 结果


@dataclass(frozen=True)
class Builtin:
    """一个平台内置 hook：可挂的点、参数校验（返回问题列表）、实现，以及按参数判断是否会调用模型（决定超时）。"""

    points: frozenset[HookPoint]
    validate: Callable[[dict[str, Any]], list[str]]
    impl: HookImpl
    calls_model: Callable[[dict[str, Any]], bool] = lambda _params: False


@dataclass(frozen=True)
class HookSpec:
    """快照中声明的一个 hook。"""

    point: HookPoint
    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookOutcome:
    """一个点上整条链的执行结论。payload 为应用覆盖后的负载；overrides 为累计生效的白名单覆盖；
    denied_by 为导致拒绝的 hook 名（None 表示放行）；records 为逐个 hook 的结论记录（可直接写入事件 payload）。"""

    payload: dict[str, Any]
    overrides: dict[str, Any] = field(default_factory=dict)
    denied_by: str | None = None
    reason: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)

    @property
    def denied(self) -> bool:
        return self.denied_by is not None


# ---------------- 内置 hook ----------------


def _positive_int(params: dict[str, Any], key: str, *, required: bool) -> list[str]:
    if key not in params:
        return [f"params.{key} is required"] if required else []
    v = params[key]
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        return [f"params.{key} must be a positive integer"]
    return []


def _only_keys(params: dict[str, Any], allowed: set[str]) -> list[str]:
    return [f"unknown param '{k}'" for k in params if k not in allowed]


def _validate_token_budget(params: dict[str, Any]) -> list[str]:
    return _only_keys(params, {"max_total_tokens"}) + _positive_int(params, "max_total_tokens", required=True)


async def _token_budget(params: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """pre_model：本 Run 累计用量（输入 + 输出）达到上限即拒绝后续模型调用。"""
    used = int((payload.get("usage") or {}).get("total_tokens", 0))
    limit = int(params["max_total_tokens"])
    if used >= limit:
        return {"deny": f"token budget exhausted ({used}/{limit})"}
    return None


SUMMARY_MODES = ("llm", "truncate")
SUMMARY_PROMPT = "用不超过 {n} 个字概括下面这段推理过程的要点，只输出概括本身，不要复述原文中的敏感数据。"


def _validate_thinking_summary(params: dict[str, Any]) -> list[str]:
    problems = _only_keys(params, {"mode", "max_chars", "model_alias"}) + _positive_int(params, "max_chars", required=False)
    if params.get("mode", "truncate") not in SUMMARY_MODES:
        problems.append(f"params.mode must be one of {', '.join(SUMMARY_MODES)}")
    if "model_alias" in params and (not isinstance(params["model_alias"], str) or not params["model_alias"]):
        problems.append("params.model_alias must be a non-empty string")
    return problems


async def _thinking_summary(params: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """post_model：把本轮思维链生成摘要（覆盖键 thinking_summary）。truncate 截断原文；llm 经现有 LiteLLM 客户端调摘要模型——
    缺省为非推理摘要别名 THINKING_SUMMARY_MODEL_ALIAS（S5 #7，D151），params.model_alias 可覆盖。Run 的数据等级不被分级矩阵的
    model 通道准许发给摘要模型时（如机密污点，S9 OPT-4）不生成（不下发）；等级 ≥ internal 的摘要请求关闭网关回退。"""
    thinking = payload.get("thinking") or ""
    if not isinstance(thinking, str) or not thinking:
        return None
    max_chars = int(params.get("max_chars", 200))
    if params.get("mode", "truncate") == "truncate":
        return {"thinking_summary": thinking if len(thinking) <= max_chars else thinking[:max_chars] + "…"}
    from runtime.loop import data_class as dc  # 延迟导入：runtime.loop 包初始化会导入 runner，而 runner 依赖本模块

    alias = str(params.get("model_alias") or settings.thinking_summary_model_alias or payload.get("model_alias") or "")
    level = dc.run_level(payload.get("taint") or [])
    if not dc.model_allowed(alias, level, settings):
        return None
    resp = await llm.get_client().chat(
        alias,
        [{"role": "system", "content": SUMMARY_PROMPT.format(n=max_chars)}, {"role": "user", "content": thinking}],
        temperature=0,
        extra=None if dc.fallbacks_allowed(alias, level, settings) else {"disable_fallbacks": True},
    )
    text = resp.text.strip()[:max_chars]
    return {"thinking_summary": text} if text else None


def _validate_deny_tool_pattern(params: dict[str, Any]) -> list[str]:
    problems = _only_keys(params, {"patterns"})
    pats = params.get("patterns")
    if not isinstance(pats, list) or not pats or not all(isinstance(p, str) and p for p in pats):
        problems.append("params.patterns must be a non-empty list of non-empty strings")
    return problems


async def _deny_tool_pattern(params: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """pre_tool：工具名命中任一通配模式（fnmatch，区分大小写）即拒绝。"""
    tool = str(payload.get("tool", ""))
    for pat in params["patterns"]:
        if fnmatch.fnmatchcase(tool, pat):
            return {"deny": f"tool '{tool}' matches deny pattern '{pat}'"}
    return None


# 顺序有意义：身份证号先于银行卡号（18 位身份证号同样落在 16～19 位数字范围内）。
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "id_card": re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])"),
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}


def _validate_redact_pii(params: dict[str, Any]) -> list[str]:
    problems = _only_keys(params, {"kinds", "data_class"})
    kinds = params.get("kinds", list(PII_PATTERNS))
    if not isinstance(kinds, list) or not kinds or any(k not in PII_PATTERNS for k in kinds):
        problems.append(f"params.kinds must be a non-empty subset of {', '.join(PII_PATTERNS)}")
    if "data_class" in params and params["data_class"] not in DATA_CLASS_RANK:
        problems.append(f"params.data_class must be one of {', '.join(DATA_CLASS_RANK)}")
    return problems


def redact_text(text: str, kinds: Iterable[str]) -> tuple[str, int]:
    """按 kinds 依次替换手机号 / 邮箱 / 身份证号 / 银行卡号为 [REDACTED:<kind>]，返回 (新文本, 替换次数)。"""
    hits = 0
    for kind in PII_PATTERNS:
        if kind in kinds:
            text, n = PII_PATTERNS[kind].subn(f"[REDACTED:{kind}]", text)
            hits += n
    return text, hits


def _redact_value(value: Any, kinds: list[str]) -> tuple[Any, int]:
    if isinstance(value, str):
        return redact_text(value, kinds)
    if isinstance(value, bool):
        return value, 0
    if isinstance(value, int):  # 数值型的手机号 / 卡号同样脱敏（整串命中才替换）
        new, n = redact_text(str(value), kinds)
        return (new, n) if n else (value, 0)
    if isinstance(value, list):
        out, total = [], 0
        for item in value:
            v, n = _redact_value(item, kinds)
            out.append(v)
            total += n
        return out, total
    if isinstance(value, dict):
        d, total = {}, 0
        for k, item in value.items():
            v, n = _redact_value(item, kinds)
            d[k] = v
            total += n
        return d, total
    return value, 0


async def _redact_pii(params: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    """post_tool：脱敏工具输出中的手机号 / 邮箱 / 身份证号 / 银行卡号；命中且配置了 data_class 时同时升级数据分级。"""
    kinds = list(params.get("kinds", list(PII_PATTERNS)))
    output, hits = _redact_value(payload.get("output"), kinds)
    if not hits:
        return None
    result: dict[str, Any] = {"output": output}
    if "data_class" in params:
        result["data_class"] = params["data_class"]
    return result


BUILTINS: dict[str, Builtin] = {
    "token_budget": Builtin(frozenset({HookPoint.PRE_MODEL}), _validate_token_budget, _token_budget),
    "thinking_summary": Builtin(
        frozenset({HookPoint.POST_MODEL}),
        _validate_thinking_summary,
        _thinking_summary,
        calls_model=lambda p: p.get("mode", "truncate") == "llm",
    ),
    "deny_tool_pattern": Builtin(frozenset({HookPoint.PRE_TOOL}), _validate_deny_tool_pattern, _deny_tool_pattern),
    "redact_pii": Builtin(frozenset({HookPoint.POST_TOOL}), _validate_redact_pii, _redact_pii),
}

DEFAULT_THINKING_SUMMARY = HookSpec(HookPoint.POST_MODEL, "thinking_summary", {})  # D4：summary 可见性下的隐式默认


# ---------------- 配置校验 ----------------


def validate_hooks_config(raw: Any) -> list[str]:
    """校验快照 config.hooks，返回问题列表（空列表表示合规；未配置视为合规）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return ["hooks must be a list"]
    problems: list[str] = []
    for i, item in enumerate(raw):
        where = f"hooks[{i}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be an object")
            continue
        extra = set(item) - {"point", "name", "params"}
        if extra:
            problems.append(f"{where}: unknown keys {sorted(extra)}")
        try:
            point = HookPoint(str(item.get("point")))
        except ValueError:
            problems.append(f"{where}: unknown point {item.get('point')!r}")
            continue
        name = item.get("name")
        builtin = BUILTINS.get(name) if isinstance(name, str) else None
        if builtin is None:
            problems.append(f"{where}: unknown hook {name!r} (only built-in hooks are allowed)")
            continue
        if point not in builtin.points:
            problems.append(f"{where}: hook '{name}' cannot run at {point.value}")
            continue
        params = item.get("params", {})
        if not isinstance(params, dict):
            problems.append(f"{where}: params must be an object")
            continue
        problems += [f"{where} ({name}): {p}" for p in builtin.validate(params)]
    return problems


def parse_hooks(raw: Any) -> list[HookSpec]:
    """把合规的 config.hooks 解析为 HookSpec 列表；不合规抛 HookConfigError。"""
    problems = validate_hooks_config(raw)
    if problems:
        raise HookConfigError("; ".join(problems))
    return [HookSpec(HookPoint(h["point"]), h["name"], dict(h.get("params", {}))) for h in raw or []]


# ---------------- 执行 ----------------


def _upgraded(current: Any, proposed: Any) -> Any:
    """post_tool data_class 只升不降：未知的提议取值按 confidential 处理（失败关闭）。"""
    proposed = proposed if proposed in DATA_CLASS_RANK else "confidential"
    cur_rank = DATA_CLASS_RANK.get(current, -1) if isinstance(current, str) else -1
    return proposed if DATA_CLASS_RANK[proposed] > cur_rank else current


class HookChain:
    """一个 Agent 版本的 hook 链（按点分组、保持声明顺序）。空链的 has() 一律为 False，调用方据此保持事件日志不变。"""

    def __init__(self, specs: list[HookSpec] | None = None) -> None:
        self.specs = list(specs or [])

    @classmethod
    def for_agent(cls, config: dict[str, Any] | None, visibility: str = "internal") -> HookChain:
        """按快照构造：解析 config.hooks（不合规抛 HookConfigError）；thinking_visibility=summary 且未声明
        thinking_summary 时隐式追加默认参数的 thinking_summary（D4）。"""
        specs = parse_hooks((config or {}).get("hooks"))
        if visibility == "summary" and not any(s.point == HookPoint.POST_MODEL and s.name == "thinking_summary" for s in specs):
            specs.append(DEFAULT_THINKING_SUMMARY)
        return cls(specs)

    def at(self, point: HookPoint) -> list[HookSpec]:
        return [s for s in self.specs if s.point == point]

    def has(self, point: HookPoint) -> bool:
        return any(s.point == point for s in self.specs)

    async def run(self, point: HookPoint, payload: dict[str, Any]) -> HookOutcome:
        """按声明顺序执行该点的 hook。每个 hook 拿到负载的深拷贝（不能就地篡改），首个 deny / 失败（post_model 除外）即停。"""
        out = HookOutcome(payload=copy.deepcopy(payload))
        allowed = HOOK_OVERRIDABLE[point]
        for spec in self.at(point):
            record: dict[str, Any] = {"point": point.value, "name": spec.name}
            out.records.append(record)
            builtin = BUILTINS.get(spec.name)
            timeout = MODEL_TIMEOUT_S if builtin is not None and builtin.calls_model(spec.params) else PURE_TIMEOUT_S
            try:
                if builtin is None:
                    raise HookConfigError(f"unknown hook {spec.name!r}")
                result = await asyncio.wait_for(builtin.impl(spec.params, copy.deepcopy(out.payload)), timeout)
                if result is not None and not isinstance(result, dict):
                    raise TypeError(f"hook returned {type(result).__name__}, expected dict or None")
            except TimeoutError:
                record |= {"verdict": "timeout", "reason": f"timed out after {timeout:g}s"}
            except Exception as exc:  # 失败关闭：hook 自身的任何异常都不放行
                record |= {"verdict": "error", "reason": type(exc).__name__}
            else:
                if not result:
                    record["verdict"] = "allow"
                    continue
                if "deny" in result:
                    record |= {"verdict": "deny", "reason": str(result["deny"])}
                    out.denied_by, out.reason = spec.name, record["reason"]
                    return out
                ignored = sorted(k for k in result if k not in allowed)
                if ignored:
                    log.warning("hook %s at %s returned non-overridable keys %s; ignored", spec.name, point.value, ignored)
                    record["ignored"] = ignored
                applied = {k: v for k, v in result.items() if k in allowed}
                if not (point == HookPoint.PRE_TOOL and "input" in applied and not isinstance(applied["input"], dict)):
                    if point == HookPoint.POST_TOOL and "data_class" in applied:
                        applied["data_class"] = _upgraded(out.payload.get("data_class"), applied["data_class"])
                    out.payload.update(applied)
                    out.overrides.update(applied)
                    record["verdict"] = "override" if applied else "allow"
                    continue
                record |= {"verdict": "error", "reason": "input override must be an object"}
            # 走到这里是 error / timeout：post_model 只记录不阻断（不覆盖即不下发摘要），其余点按 deny 处理
            if point == HookPoint.POST_MODEL:
                continue
            out.denied_by, out.reason = spec.name, str(record.get("reason"))
            return out
        return out

    def hookset(self, points: Iterable[HookPoint], sink: list[dict[str, Any]] | None = None) -> HookSet:
        """传给适配器 run_turn 的 HookSet：只含 points（适配器声明的 internal_hook_points）中本链配置了 hook 的点。
        返回值遵循契约：拒绝为 {"deny": reason}，否则为白名单内的覆盖（可能为 None）。sink 收集记录（标 source=adapter）。"""
        fns: dict[str, HookFn] = {}
        for point in points:
            if point in HOOK_OVERRIDABLE and self.has(point):
                fns[point.value] = self._as_hookfn(point, sink)
        return HookSet(**fns)

    def _as_hookfn(self, point: HookPoint, sink: list[dict[str, Any]] | None) -> HookFn:
        async def fn(payload: dict[str, Any]) -> dict[str, Any] | None:
            outcome = await self.run(point, payload)
            if sink is not None:
                sink.extend(r | {"source": "adapter"} for r in outcome.records)
            if outcome.denied:
                return {"deny": f"hook:{outcome.denied_by}"}
            return dict(outcome.overrides) or None

        return fn
