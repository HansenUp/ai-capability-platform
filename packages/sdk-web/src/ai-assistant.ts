/**
 * `<ai-assistant>` 嵌入式对话组件（Lit Web Component）。
 *
 * 平台"AI 触点层"的通用嵌入方式：任何业务页面加一个标签即可接入 Agent。
 * 职责：
 * - 调用 Runtime 的 `POST /v1/messages`（SSE 流式）并逐事件渲染；
 * - 处理统一事件流中的 text / card / done（thinking、tool_use、tool_result、usage、error 当前忽略，后续版本展示）；
 * - 收到 `card`（impact_preview 影响预演）时渲染审批卡片，点击"通过"调用 `POST /v1/runs/{run_id}/approve`；
 * - 通过 `tokenProvider` 附加用户 OAuth 令牌，生产环境由 APISIX 验签并注入身份；
 * - 每条助手回复结束后显示反馈控件（点赞 / 点踩 / 修正），调用 `POST /v1/runs/{run_id}/feedback`，
 *   由 Runtime 写成 Langfuse score 挂到该 Run 的 Trace 上（S3.5）。
 */
import { LitElement, css, html } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { components } from "./types/runtime";

/** SSE data 帧的类型：由 docs/api/runtime-openapi.json 生成的契约统一事件（按 type 判别的联合，见 pnpm gen:types）。 */
type Ev = components["schemas"]["Event"];
/** 审批卡片事件（impact_preview 等），payload 为卡片渲染数据。 */
type CardEv = components["schemas"]["CardEvent"];

/** 一条对话记录。feedback：已提交的反馈（up / down / correction），correcting：正在填写修正。 */
interface Item { role: string; text?: string; card?: CardEv; runId?: string; feedback?: string; correcting?: boolean }

/** 用法：<ai-assistant agent-id="demo" base-url="https://gw.example.com"></ai-assistant>
 *  通过 `tokenProvider`（async () => string）提供用户 OAuth 令牌。 */
@customElement("ai-assistant")
export class AiAssistant extends LitElement {
  static styles = css`
    :host { display: block; font: 14px/1.5 system-ui, sans-serif; border: 1px solid #e5e7eb; border-radius: 12px; overflow: hidden; }
    .log { max-height: 360px; overflow: auto; padding: 12px; background: #f8fafc; }
    .m { margin: 6px 0; } .a { color: #1f2329; } .u { color: #2563eb; text-align: right; }
    .card { border: 1px dashed #c2410c; padding: 8px; border-radius: 8px; background: #fff7ed; }
    form { display: flex; gap: 8px; padding: 8px; border-top: 1px solid #e5e7eb; }
    input { flex: 1; padding: 8px; border: 1px solid #cbd5e1; border-radius: 8px; }
    button { padding: 8px 12px; border-radius: 8px; border: 0; background: #2563eb; color: #fff; }
    .fb { display: flex; gap: 6px; align-items: center; font-size: 12px; color: #646a73; }
    .fb button { padding: 2px 8px; background: #e5e7eb; color: #1f2329; }
    .fb input { flex: 1; padding: 4px 6px; }
    .err { color: #b91c1c; }
  `;
  /** 目标 Agent id（使用已发布版本）。 */
  @property({ attribute: "agent-id" }) agentId = "";
  /** Runtime / 网关地址；同源部署时留空。 */
  @property({ attribute: "base-url" }) baseUrl = "";
  /** 令牌提供者：返回用户访问令牌，组件以 Bearer 方式附加。 */
  @property({ attribute: false }) tokenProvider?: () => Promise<string>;
  /** 对话记录：u = 用户，a = 助手，card = 审批卡片，err = 错误提示；助手消息结束后带 runId，用于反馈。 */
  @state() private items: Item[] = [];
  /** 会话 id：取自上一轮 done 事件的 run_id，用于多轮续接。 */
  @state() private sessionId?: string;

  /** 组装请求头；有 tokenProvider 时附加 Authorization。 */
  private async headers() {
    const h: Record<string, string> = { "content-type": "application/json" };
    if (this.tokenProvider) h["authorization"] = `Bearer ${await this.tokenProvider()}`;
    return h;
  }

  /** 发送一条用户消息并消费 SSE 事件流。 */
  async send(text: string) {
    this.items = [...this.items, { role: "u", text }];
    let res: Response;
    try {
      res = await fetch(`${this.baseUrl}/v1/messages`, {
        method: "POST", headers: await this.headers(),
        body: JSON.stringify({ agent_id: this.agentId, session_id: this.sessionId, messages: [{ role: "user", content: text }], stream: true }),
      });
    } catch (e) {
      // 令牌获取失败（宿主可能已跳转登录）或网络错误：提示而不是抛出未捕获异常
      this.items = [...this.items, { role: "err", text: `请求失败：${e instanceof Error ? e.message : String(e)}` }];
      return;
    }
    if (!res.ok || !res.body) { this.items = [...this.items, { role: "err", text: `请求失败：HTTP ${res.status}` }]; return; }
    // 逐块读取响应体，按行切分 SSE 帧；只解析 data: 行
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = ""; const cur: Item = { role: "a", text: "" };
    this.items = [...this.items, cur];
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n"); buf = lines.pop() ?? "";  // 末尾可能是不完整的一行，留到下一块
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const ev: Ev = JSON.parse(line.slice(5));
        if (ev.type === "text") { cur.text += ev.delta; this.items = [...this.items]; }          // 增量文本追加到当前助手消息
        else if (ev.type === "card") { this.items = [...this.items, { role: "card", card: ev }]; } // 审批卡片（impact_preview）
        else if (ev.type === "done") { this.sessionId = ev.run_id; cur.runId = ev.run_id; this.items = [...this.items]; } // run_id：会话延续与反馈
      }
    }
  }

  /** 审批通过：调用 Run API 的 approve；之后由平台以发起人凭据自动续跑（S7），可用 GET /v1/runs/{run_id}/stream 续读事件。
   *  params_hash 必填，取自卡片 payload：审批绑定具体参数，指纹不一致时 Runtime 返回 409。 */
  async approve(ev: CardEv) {
    const p = ev.payload ?? {};
    const body = { call_id: p.call_id, params_hash: p.params_hash };
    const res = await fetch(`${this.baseUrl}/v1/runs/${p.run_id}/approve`, { method: "POST", headers: await this.headers(), body: JSON.stringify(body) }).catch(() => null);
    if (!res?.ok) {
      // 403：发起人不能审批自己的 Run（需由其他人员审批）
      this.items = [...this.items, { role: "err", text: `审批失败：HTTP ${res?.status ?? "network"}${res?.status === 403 ? "（不能审批自己发起的操作）" : ""}` }];
    }
  }

  /** 提交反馈：score +1 / -1，correction 为用户给出的正确答案（视为负反馈）。 */
  async feedback(item: Item, score: number, correction?: string) {
    const body = { score, correction: correction || null };
    let status = 0;
    try {
      const res = await fetch(`${this.baseUrl}/v1/runs/${item.runId}/feedback`, { method: "POST", headers: await this.headers(), body: JSON.stringify(body) });
      status = res.ok ? 200 : res.status;
    } catch { status = 0; }
    item.correcting = false;
    item.feedback = status === 200 ? (correction ? "correction" : score > 0 ? "up" : "down") : `failed:${status || "network"}`;
    this.items = [...this.items];
  }

  /** 助手消息下方的反馈控件。 */
  private feedbackBar(i: Item) {
    if (!i.runId) return null;
    if (i.feedback) return html`<div class="fb">${i.feedback.startsWith("failed") ? html`<span class="err">反馈提交失败（${i.feedback.slice(7)}）</span>` : "已反馈，谢谢"}</div>`;
    if (i.correcting) {
      return html`<form class="fb" @submit=${(e: Event) => { e.preventDefault(); const v = (e.target as HTMLFormElement).querySelector("input")!.value; this.feedback(i, -1, v); }}>
        <input placeholder="正确答案应为…" /><button>提交修正</button></form>`;
    }
    return html`<div class="fb">
      <button title="有帮助" @click=${() => this.feedback(i, 1)}>👍</button>
      <button title="没帮助" @click=${() => this.feedback(i, -1)}>👎</button>
      <button @click=${() => { i.correcting = true; this.items = [...this.items]; }}>修正</button></div>`;
  }

  /** 渲染对话记录与输入框。 */
  render() {
    return html`
      <div class="log">${this.items.map((i) => i.role === "card"
        ? html`<div class="m card">审批：${i.card!.payload?.tool} · 影响 ${JSON.stringify(i.card!.payload?.preview)} <button @click=${() => this.approve(i.card!)}>通过</button></div>`
        : html`<div class="m ${i.role}">${i.text}</div>${i.role === "a" ? this.feedbackBar(i) : null}`)}</div>
      <form @submit=${(e: Event) => { e.preventDefault(); const inp = (e.target as HTMLFormElement).querySelector("input")!; this.send(inp.value); inp.value = ""; }}>
        <input placeholder="输入…" /><button>发送</button>
      </form>`;
  }
}
