/**
 * `<ai-assistant>` 嵌入式对话组件（Lit Web Component）。
 *
 * 平台"AI 触点层"的通用嵌入方式：任何业务页面加一个标签即可接入 Agent。
 * 职责：
 * - 调用 Runtime 的 `POST /v1/messages`（SSE 流式）并逐事件渲染；
 * - 处理统一事件流中的 text / card / done（thinking、tool_use、tool_result、usage、error 当前忽略，后续版本展示）；
 * - 收到 `card`（impact_preview 影响预演）时渲染审批卡片，点击"通过"调用 `POST /v1/runs/{run_id}/approve`；
 * - 通过 `tokenProvider` 附加用户 OAuth 令牌，生产环境由 APISIX 验签并注入身份。
 */
import { LitElement, css, html } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import type { components } from "./types/runtime";

/** SSE data 帧的类型：由 docs/api/runtime-openapi.json 生成的契约统一事件（按 type 判别的联合，见 pnpm gen:types）。 */
type Ev = components["schemas"]["Event"];
/** 审批卡片事件（impact_preview 等），payload 为卡片渲染数据。 */
type CardEv = components["schemas"]["CardEvent"];

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
  `;
  /** 目标 Agent id（使用已发布版本）。 */
  @property({ attribute: "agent-id" }) agentId = "";
  /** Runtime / 网关地址；同源部署时留空。 */
  @property({ attribute: "base-url" }) baseUrl = "";
  /** 令牌提供者：返回用户访问令牌，组件以 Bearer 方式附加。 */
  @property({ attribute: false }) tokenProvider?: () => Promise<string>;
  /** 对话记录：u = 用户，a = 助手，card = 审批卡片。 */
  @state() private items: { role: string; text?: string; card?: CardEv }[] = [];
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
    const res = await fetch(`${this.baseUrl}/v1/messages`, {
      method: "POST", headers: await this.headers(),
      body: JSON.stringify({ agent_id: this.agentId, session_id: this.sessionId, messages: [{ role: "user", content: text }], stream: true }),
    });
    // 逐块读取响应体，按行切分 SSE 帧；只解析 data: 行
    const reader = res.body!.getReader(); const dec = new TextDecoder(); let buf = ""; let cur = { role: "a", text: "" };
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
        else if (ev.type === "done") { this.sessionId = ev.run_id; }                              // 记录 run_id 作为会话延续
      }
    }
  }

  /** 审批通过：调用 Run API 的 approve；之后由宿主页面或后续版本调用 resume 继续执行。
   *  params_hash 必填，取自卡片 payload：审批绑定具体参数，指纹不一致时 Runtime 返回 409。 */
  async approve(ev: CardEv) {
    const p = ev.payload ?? {};
    const body = { call_id: p.call_id, params_hash: p.params_hash };
    const res = await fetch(`${this.baseUrl}/v1/runs/${p.run_id}/approve`, { method: "POST", headers: await this.headers(), body: JSON.stringify(body) });
    if (!res.ok) throw new Error(`approve failed: HTTP ${res.status}`);
  }

  /** 渲染对话记录与输入框。 */
  render() {
    return html`
      <div class="log">${this.items.map((i) => i.role === "card"
        ? html`<div class="m card">审批：${i.card!.payload?.tool} · 影响 ${JSON.stringify(i.card!.payload?.preview)} <button @click=${() => this.approve(i.card!)}>通过</button></div>`
        : html`<div class="m ${i.role}">${i.text}</div>`)}</div>
      <form @submit=${(e: Event) => { e.preventDefault(); const inp = (e.target as HTMLFormElement).querySelector("input")!; this.send(inp.value); inp.value = ""; }}>
        <input placeholder="输入…" /><button>发送</button>
      </form>`;
  }
}
