import type { components } from "./types/runtime";
export * from "./run-client";
export declare class AiAssistant extends HTMLElement {
  agentId: string;
  baseUrl: string;
  tokenProvider?: () => Promise<string>;
  send(text: string): Promise<void>;
  approve(event: components["schemas"]["CardEvent"]): Promise<void>;
  feedback(item: { role: string; runId?: string; feedback?: string; correcting?: boolean }, score: number, correction?: string): Promise<void>;
}
