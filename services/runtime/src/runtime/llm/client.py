"""经 LiteLLM（Model Gateway）的 OpenAI 兼容 LLM 客户端。

职责边界：
- 只和网关的 OpenAI 兼容接口打交道（`/v1/chat/completions`、`/model/info`），不直连任何厂商；
  模型别名到底层模型的路由、回退（fallback）、计费都由网关负责。
- 流式调用把 SSE 增量解析为 `LLMChunk`：推理（thinking）/ 文本增量即时产出，工具调用按 index
  聚合后在流结束时产出，最后固定产出一条 usage 与一条 done。
- 超时与重试：连接错误、超时、429、5xx 按指数退避重试，最多 `settings.llm_max_retries` 次；
  流式下只在**尚未产出任何增量**之前重试——已经把部分内容交给调用方后再重试会造成内容重复，
  因此改为抛出 `LLMError`（带 retryable 标志），是否整轮重来由上层决定。
  httpx 的全部异常（`httpx.HTTPError`）都收口为 `LLMError`：网络层 / 超时可重试，其余（如响应解码失败）不可重试。
- 别名解析：`/model/info` 只请求一次、不重试；成功结果缓存 model_info_ttl_s 秒，失败结果（网络、401/403 等）
  缓存 model_info_negative_ttl_s 秒，期间直接抛出缓存的失败，避免每轮对话都去撞一个已知不可用的接口。
- 历史翻译：`to_openai_messages` 把 runner 的历史消息（见 runtime.run.invariant 的几种形态）
  翻译为 OpenAI 消息格式；`to_openai_tools` 把 ToolSpec 翻译为 OpenAI function 工具声明。

测试注入点：`LLMClient(transport=...)` 可直接传入 httpx 传输层（如 `httpx.MockTransport`）；
进程级默认客户端经 `get_client()` 获取，测试可用 `set_transport_for_tests()` 替换其传输层。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from ai_contract import ToolSpec
from runtime.config import settings

CHAT_PATH = "/v1/chat/completions"  # OpenAI 兼容的对话补全接口
MODEL_INFO_PATH = "/model/info"  # LiteLLM 的模型（别名）清单接口
MAX_BACKOFF_S = 10.0  # 单次退避等待上限（秒）
ERROR_BODY_LIMIT = 500  # 错误信息中保留的响应体字符数

# chunk 类型：thinking 推理增量 / text 文本增量 / tool_call 聚合完成的工具调用 / usage 用量 / done 结束
ChunkKind = Literal["thinking", "text", "tool_call", "usage", "done"]


class LLMError(RuntimeError):
    """模型调用失败。

    - retryable：该错误本身是否属于可重试类别（网络、超时、429、5xx）；客户端内部已按配置重试过，
      抛到上层时仍为 True 表示"重试次数耗尽"或"流已部分产出、不便自动重试"，上层可自行决定是否整轮重来；
    - status_code：网关返回的 HTTP 状态码（网络层错误时为 None）；
    - code：统一错误码，超时为 LLM_TIMEOUT，其余为 LLM_ERROR，直接映射到 ErrorEvent.code。
    """

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None, code: str = "LLM_ERROR") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.code = code


class UnknownModelAlias(LookupError):
    """网关的 `/model/info` 中找不到给定的模型别名。"""

    def __init__(self, alias: str) -> None:
        super().__init__(f"unknown model alias '{alias}'（网关未配置该模型别名）")
        self.alias = alias


@dataclass(frozen=True)
class ModelInfo:
    """模型别名在网关中的解析结果。"""

    alias: str  # 平台使用的模型别名（网关的 model_name）
    model: str  # 底层真实模型（litellm_params.model，如 deepseek/deepseek-v4-pro）
    provider: str | None = None  # 模型供应商（model_info.litellm_provider）
    max_input_tokens: int | None = None  # 最大输入 token 数
    max_output_tokens: int | None = None  # 最大输出 token 数


@dataclass(frozen=True)
class ToolCall:
    """一次聚合完成的工具调用。arguments 解析失败时为 {"_raw": 原始字符串}，不抛异常。"""

    id: str  # 模型给出的调用标识（tool_call_id）
    name: str  # 工具名（OpenAI 侧的函数名，可能是经 to_openai_tool_name 转换后的名字）
    arguments: dict[str, Any] = field(default_factory=dict)  # 解析后的入参


@dataclass(frozen=True)
class LLMUsage:
    """一次模型调用的用量。output_tokens 取 completion_tokens，已包含推理 token。"""

    input_tokens: int = 0  # prompt_tokens
    output_tokens: int = 0  # completion_tokens（含推理 token）
    cache_read_tokens: int = 0  # prompt_tokens_details.cached_tokens
    # 推理 token：优先取 completion_tokens_details.reasoning_tokens，缺失时按推理文本长度 //3 估算，
    # 用于把 reasoning_content 纳入上下文预算统计。
    reasoning_tokens: int = 0
    cost: float = 0.0  # 网关计算的本次调用成本（usage.cost），缺失为 0


@dataclass(frozen=True)
class LLMChunk:
    """流式调用产出的一个增量单元。

    产出顺序：thinking*/text*（按到达顺序交错）→ tool_call*（流结束后按 index 顺序）→ usage（恰好一条）
    → done（恰好一条，携带 finish_reason 与响应中的 model）。
    """

    kind: ChunkKind
    delta: str = ""  # thinking / text 的增量文本
    tool_call: ToolCall | None = None  # kind == "tool_call" 时有值
    usage: LLMUsage | None = None  # kind == "usage" 时有值
    finish_reason: str | None = None  # kind == "done" 时为 stop / tool_calls / length 等
    model: str | None = None  # kind 为 usage / done 时为响应里的 model 字段


@dataclass(frozen=True)
class LLMResponse:
    """非流式调用的完整结果。"""

    text: str  # 正文
    thinking: str  # 推理内容（reasoning_content）
    tool_calls: list[ToolCall]  # 工具调用列表
    usage: LLMUsage  # 用量
    finish_reason: str | None  # 结束原因
    model: str | None  # 响应里的 model 字段


# ---------------- 解析辅助 ----------------


def _new_call_id() -> str:
    """上游未给出工具调用 id 时的兜底 id：全局唯一，避免不同轮次的 call_0 / call_1 在历史与事件日志中撞车。"""
    return f"call_{uuid.uuid4().hex[:24]}"


def _parse_arguments(raw: str) -> dict[str, Any]:
    """把工具调用的 arguments 字符串解析为 dict；空串视为 {}，非法 JSON 或非对象返回 {"_raw": raw}。"""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"_raw": raw}


def _int(value: Any) -> int:
    """把用量字段宽松转换为 int（None / 非数字一律为 0）。"""
    return int(value) if isinstance(value, int | float) else 0


def _usage_from(raw: Mapping[str, Any] | None, reasoning_text: str) -> LLMUsage:
    """从 OpenAI 格式的 usage 字典构造 LLMUsage；reasoning_tokens 缺失时按推理文本长度估算。"""
    raw = raw or {}
    completion_details = raw.get("completion_tokens_details") or {}
    prompt_details = raw.get("prompt_tokens_details") or {}
    reported = completion_details.get("reasoning_tokens")
    cost = raw.get("cost")
    return LLMUsage(
        input_tokens=_int(raw.get("prompt_tokens")),
        output_tokens=_int(raw.get("completion_tokens")),
        cache_read_tokens=_int(prompt_details.get("cached_tokens")),
        reasoning_tokens=_int(reported) if isinstance(reported, int | float) else len(reasoning_text) // 3,
        cost=float(cost) if isinstance(cost, int | float) else 0.0,
    )


def _reasoning_of(obj: Mapping[str, Any]) -> str:
    """取推理增量：DeepSeek 等推理模型给 reasoning_content，部分网关/厂商给 reasoning，两者都兼容。"""
    value = obj.get("reasoning_content") or obj.get("reasoning")
    return value if isinstance(value, str) else ""


@dataclass
class _ToolBuf:
    """流式工具调用的分片缓冲：id / name 取首个非空值，arguments 按到达顺序拼接。"""

    id: str = ""
    name: str = ""
    args: list[str] = field(default_factory=list)


class _StreamState:
    """单次流式响应的解析状态。feed 处理一个 SSE data 负载，finish 在流结束时产出收尾 chunk。"""

    def __init__(self) -> None:
        self.model: str | None = None
        self.finish_reason: str | None = None
        self.usage_raw: Mapping[str, Any] | None = None
        self.reasoning: list[str] = []
        self.tools: dict[int, _ToolBuf] = {}

    def feed(self, payload: Mapping[str, Any]) -> list[LLMChunk]:
        """解析一个 chunk 负载，返回其中可立即产出的 thinking / text 增量。"""
        if "error" in payload:  # 网关在流中途下发的错误帧（如上游厂商报错）
            detail = json.dumps(payload["error"], ensure_ascii=False)[:ERROR_BODY_LIMIT]
            raise LLMError(f"gateway stream error: {detail}", retryable=False)
        if isinstance(payload.get("model"), str):
            self.model = payload["model"]
        if payload.get("usage"):
            self.usage_raw = payload["usage"]
        out: list[LLMChunk] = []
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = _reasoning_of(delta)
            if reasoning:
                self.reasoning.append(reasoning)
                out.append(LLMChunk(kind="thinking", delta=reasoning))
            content = delta.get("content")
            if isinstance(content, str) and content:
                out.append(LLMChunk(kind="text", delta=content))
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                buf = self.tools.setdefault(idx if isinstance(idx, int) else 0, _ToolBuf())
                fn = tc.get("function") or {}
                if tc.get("id") and not buf.id:
                    buf.id = str(tc["id"])
                if fn.get("name") and not buf.name:
                    buf.name = str(fn["name"])
                if isinstance(fn.get("arguments"), str):
                    buf.args.append(fn["arguments"])
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])
        return out

    def finish(self) -> list[LLMChunk]:
        """流结束：按 index 顺序产出聚合后的工具调用，再产出 usage 与 done。"""
        out = [
            LLMChunk(
                kind="tool_call",
                tool_call=ToolCall(id=buf.id or _new_call_id(), name=buf.name, arguments=_parse_arguments("".join(buf.args))),
            )
            for _, buf in sorted(self.tools.items())
        ]
        out.append(LLMChunk(kind="usage", usage=_usage_from(self.usage_raw, "".join(self.reasoning)), model=self.model))
        out.append(LLMChunk(kind="done", finish_reason=self.finish_reason, model=self.model))
        return out


def _status_error(status: int, body: str) -> LLMError:
    """按 HTTP 状态码构造 LLMError：429 与 5xx 可重试，408 视为超时，其余 4xx 不可重试。"""
    retryable = status == 429 or status >= 500 or status == 408
    code = "LLM_TIMEOUT" if status in (408, 504) else "LLM_ERROR"
    return LLMError(f"model gateway returned HTTP {status}: {body[:ERROR_BODY_LIMIT]}", retryable=retryable, status_code=status, code=code)


def _http_error(exc: httpx.HTTPError) -> LLMError:
    """把 httpx 异常转换为 LLMError：超时（LLM_TIMEOUT）与其余网络层错误可重试；
    非网络层错误（响应解码失败、重定向过多等）重试也无济于事，标记为不可重试。"""
    if isinstance(exc, httpx.TimeoutException):
        err = LLMError(f"model gateway timeout: {exc!r}", retryable=True, code="LLM_TIMEOUT")
    elif isinstance(exc, httpx.TransportError):
        err = LLMError(f"model gateway unreachable: {exc!r}", retryable=True)
    else:
        err = LLMError(f"model gateway HTTP error: {exc!r}", retryable=False)
    err.__cause__ = exc
    return err


def _parse_model_info(data: Any) -> dict[str, ModelInfo]:
    """解析 `/model/info` 响应为 {别名: ModelInfo}；同名多部署（负载均衡）时取第一条。"""
    out: dict[str, ModelInfo] = {}
    entries = data.get("data") if isinstance(data, dict) else None
    for entry in entries or []:
        alias = entry.get("model_name")
        if not isinstance(alias, str) or alias in out:
            continue
        params = entry.get("litellm_params") or {}
        info = entry.get("model_info") or {}
        out[alias] = ModelInfo(
            alias=alias,
            model=str(params.get("model") or alias),
            provider=info.get("litellm_provider"),
            max_input_tokens=info.get("max_input_tokens"),
            max_output_tokens=info.get("max_output_tokens"),
        )
    return out


# ---------------- 客户端 ----------------


class LLMClient:
    """经 LiteLLM 网关的 OpenAI 兼容客户端。参数缺省时取 runtime.config.settings。

    每次请求创建独立的 httpx.AsyncClient：模型请求耗时远大于建连开销，而这样可以避免连接池跨事件循环
    复用的问题（FastAPI TestClient、后台任务等可能运行在不同的事件循环上）。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout_s: float | None = None,
        max_retries: int | None = None,
        backoff_s: float = 0.5,
        model_info_ttl_s: float = 60.0,
        model_info_negative_ttl_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or settings.model_gateway_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.model_gateway_api_key
        self.timeout_s = timeout_s if timeout_s is not None else settings.llm_timeout_s
        self.max_retries = max_retries if max_retries is not None else settings.llm_max_retries
        self.backoff_s = backoff_s  # 首次重试前的等待（秒），之后每次翻倍
        self.model_info_ttl_s = model_info_ttl_s  # 别名解析缓存有效期（秒）
        self.model_info_negative_ttl_s = model_info_negative_ttl_s  # `/model/info` 失败结果的缓存有效期（秒）
        self._transport = transport
        self._models: dict[str, ModelInfo] | None = None  # 别名解析缓存
        self._models_at = 0.0  # 缓存写入时刻（time.monotonic）
        self._models_error: LLMError | None = None  # 最近一次 `/model/info` 失败（负缓存）
        self._models_error_at = 0.0  # 失败发生时刻（time.monotonic）

    def _http(self) -> httpx.AsyncClient:
        """构造一次性的 httpx 客户端（Bearer 鉴权、统一超时、可注入传输层）。"""
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(self.timeout_s),
            transport=self._transport,
        )

    def _delay(self, attempt: int) -> float:
        """第 attempt 次重试前的指数退避时长。"""
        return float(min(self.backoff_s * 2 ** (attempt - 1), MAX_BACKOFF_S))

    @staticmethod
    def _body(
        model_alias: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
        temperature: float | None,
        extra: Mapping[str, Any] | None,
        stream: bool,
    ) -> dict[str, Any]:
        """构造 chat/completions 请求体（流式与非流式共用）；extra 中的字段最后合并，可覆盖前面的默认值。"""
        body: dict[str, Any] = {"model": model_alias, "messages": messages}
        if tools:
            body["tools"] = tools
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}  # 让最后一个 chunk 携带 usage
        if extra:
            body.update(extra)
        return body

    async def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None, *, max_retries: int | None = None) -> Any:
        """发起一次非流式请求并返回 JSON，按重试策略处理可重试错误；max_retries 缺省取客户端配置。"""
        retries = self.max_retries if max_retries is None else max_retries
        attempt = 0
        while True:
            try:
                async with self._http() as http:
                    resp = await http.request(method, path, json=body)
                if resp.status_code >= 400:
                    raise _status_error(resp.status_code, resp.text)
                try:
                    return resp.json()
                except ValueError as e:
                    raise LLMError(f"model gateway returned non-JSON body: {resp.text[:ERROR_BODY_LIMIT]}", retryable=False) from e
            except LLMError as e:
                err = e
            except httpx.HTTPError as e:
                err = _http_error(e)
            if not err.retryable or attempt >= retries:
                raise err
            attempt += 1
            await asyncio.sleep(self._delay(attempt))

    async def stream_chat(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> AsyncGenerator[LLMChunk, None]:
        """流式对话补全。tools 为 OpenAI 格式的工具声明（见 to_openai_tools）。

        只在尚未产出任何 chunk 之前重试；一旦产出过增量，后续失败直接抛 LLMError。
        """
        body = self._body(model_alias, messages, tools, max_tokens, temperature, extra, stream=True)
        attempt = 0
        produced = False
        while True:
            try:
                async with self._http() as http, http.stream("POST", CHAT_PATH, json=body) as resp:
                    if resp.status_code >= 400:
                        raw = await resp.aread()
                        raise _status_error(resp.status_code, raw.decode("utf-8", errors="replace"))
                    state = _StreamState()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue  # 空行、SSE 注释（:keep-alive）与 event: 行均忽略
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            payload = json.loads(data)
                        except ValueError as e:
                            raise LLMError(f"malformed SSE data from gateway: {data[:ERROR_BODY_LIMIT]}", retryable=False) from e
                        for chunk in state.feed(payload):
                            produced = True
                            yield chunk
                    for chunk in state.finish():
                        produced = True
                        yield chunk
                return
            except LLMError as e:
                err = e
            except httpx.HTTPError as e:
                err = _http_error(e)
            if produced or not err.retryable or attempt >= self.max_retries:
                raise err
            attempt += 1
            await asyncio.sleep(self._delay(attempt))

    async def chat(
        self,
        model_alias: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        """非流式对话补全，与 stream_chat 共用请求构造与重试策略。"""
        body = self._body(model_alias, messages, tools, max_tokens, temperature, extra, stream=False)
        data = await self._request_json("POST", CHAT_PATH, body)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        thinking = _reasoning_of(message)
        tool_calls = [
            ToolCall(
                id=str(tc.get("id") or _new_call_id()),
                name=str((tc.get("function") or {}).get("name") or ""),
                arguments=_parse_arguments(str((tc.get("function") or {}).get("arguments") or "")),
            )
            for tc in message.get("tool_calls") or []
        ]
        return LLMResponse(
            text=message.get("content") or "",
            thinking=thinking,
            tool_calls=tool_calls,
            usage=_usage_from(data.get("usage"), thinking),
            finish_reason=choice.get("finish_reason"),
            model=data.get("model"),
        )

    async def resolve_alias(self, model_alias: str) -> ModelInfo:
        """经网关 `GET /model/info` 解析模型别名。找不到抛 UnknownModelAlias，请求失败抛 LLMError。

        该接口只用于给 UsageEvent 标注底层模型，属于尽力而为：请求不重试、不退避；成功的清单缓存
        model_info_ttl_s 秒（期间未知别名也不会重新请求），失败缓存 model_info_negative_ttl_s 秒。
        """
        now = time.monotonic()
        if self._models is None or now - self._models_at > self.model_info_ttl_s:
            cached = self._models_error
            if cached is not None and now - self._models_error_at < self.model_info_negative_ttl_s:
                raise LLMError(
                    f"{cached}（负缓存，暂不重试 /model/info）",
                    retryable=cached.retryable,
                    status_code=cached.status_code,
                    code=cached.code,
                )
            try:
                data = await self._request_json("GET", MODEL_INFO_PATH, max_retries=0)
            except LLMError as e:
                self._models_error, self._models_error_at = e, now
                raise
            self._models, self._models_at, self._models_error = _parse_model_info(data), now, None
        info = self._models.get(model_alias)
        if info is None:
            raise UnknownModelAlias(model_alias)
        return info


# ---------------- 进程级默认客户端（含测试注入点） ----------------

_default_client: LLMClient | None = None
_test_transport: httpx.AsyncBaseTransport | None = None


def get_client() -> LLMClient:
    """返回进程级默认客户端（按 settings 懒构造）。harness 每轮调用时获取，便于测试替换传输层。"""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient(transport=_test_transport)
    return _default_client


def set_transport_for_tests(transport: httpx.AsyncBaseTransport | None, **client_kwargs: Any) -> None:
    """测试注入点：替换默认客户端的传输层（传 None 恢复真实网络）。client_kwargs 透传给 LLMClient（如 backoff_s=0）。"""
    global _default_client, _test_transport
    _test_transport = transport
    _default_client = LLMClient(transport=transport, **client_kwargs) if transport is not None else None


# ---------------- 历史 / 工具翻译 ----------------

_INVALID_TOOL_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def to_openai_tool_name(name: str) -> str:
    """把平台工具名转换为 OpenAI / Anthropic 接受的函数名（仅允许 [A-Za-z0-9_-]，最长 64）。

    平台工具名常带点号（如 ticket.search），厂商侧会直接拒绝；点号转为双下划线，其余非法字符转为下划线。
    反向映射由 harness 依据本轮工具列表建立，不依赖该转换可逆。
    """
    return _INVALID_TOOL_NAME_CHARS.sub("_", name.replace(".", "__"))[:64]


def _tool_content(content: Any) -> str:
    """工具结果统一转为字符串：字符串原样，None 为空串，其余 JSON 序列化（保留中文）。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, default=str)


def to_openai_messages(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 runner 的历史消息翻译为 OpenAI 消息格式。

    - `{"role":"assistant","tool_use":{call_id,name,input}}` → assistant 消息的 tool_calls；
      若紧邻的前一条是 assistant 文本（或已带 tool_calls 的 assistant），合并为同一条消息；
    - `{"role":"tool","call_id","content"}` → `{"role":"tool","tool_call_id","content": 字符串}`；
    - system / user / assistant 文本原样（浅拷贝，不修改入参）。

    推理回传：tool_use 消息可带可选的顶层 `reasoning` 字段（发起该工具调用那一轮的思维链，由 runner 依事件日志
    重建）。DeepSeek 等推理模型在思考模式下要求回传它，否则返回 400；有值时附为对应 assistant tool_calls
    消息的 reasoning_content，没有则不输出该字段。
    """
    out: list[dict[str, Any]] = []
    for m in history:
        role = m.get("role")
        if role == "assistant" and isinstance(m.get("tool_use"), dict):
            tu = m["tool_use"]
            call_id = str(tu.get("call_id"))
            call = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": to_openai_tool_name(str(tu.get("name"))),
                    "arguments": json.dumps(tu.get("input") or {}, ensure_ascii=False),
                },
            }
            prev = out[-1] if out else None
            if prev is not None and prev.get("role") == "assistant" and (isinstance(prev.get("content"), str) or "tool_calls" in prev):
                prev.setdefault("tool_calls", []).append(call)
            else:
                prev = {"role": "assistant", "content": None, "tool_calls": [call]}
                out.append(prev)
            thought = m.get("reasoning")
            if isinstance(thought, str) and thought and "reasoning_content" not in prev:
                prev["reasoning_content"] = thought
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": str(m.get("call_id")), "content": _tool_content(m.get("content"))})
        else:
            out.append(dict(m))
    return out


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """把 ToolSpec 列表翻译为 OpenAI function 工具声明；input_schema 为空时给出空对象 schema。"""
    return [
        {
            "type": "function",
            "function": {
                "name": to_openai_tool_name(t.name),
                "description": t.description,
                "parameters": t.input_schema or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]
