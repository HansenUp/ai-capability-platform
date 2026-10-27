---
name: ticket-summary
description: 对售后工单进行摘要、归因并整理可复用经验。仅做只读检索与总结，不执行任何写操作。
metadata:
  version: "1.0.0"
  ai-platform.capabilities: "cap.ticket.search cap.ticket.get"
  ai-platform.risk-level: read
  ai-platform.data-class: internal
  ai-platform.sandbox: read-only
---

# 工单摘要

## 何时使用
用户要求总结、归因或沉淀某个或某批工单时。

## 步骤
1. 用 `ticket.search` 按关键字与条件定位工单（只读）。用户已给出工单号时可跳过这一步。
2. 用 `ticket.get` 按工单号取详情与处理记录（只读）；多张工单逐张获取。若返回未找到或无权限，如实告知用户无法访问，不得猜测内容。
3. 按 `references/format.md` 的模板输出：问题、根因、处理过程、客户反馈、可复用经验。

## 边界
本 Skill 只读，从不执行写操作。用户要求关闭工单、通知客户等写操作时，必须明确告知本场景不支持，请其在工单系统中自行操作；"沉淀"仅指输出可复用经验文本，不自动入库。

## 依赖能力
- cap.ticket.search（只读）
- cap.ticket.get（只读）

## 辅助脚本
`scripts/summarize.py`（只读沙箱、无网络）：从标准输入读 `{"tickets": [...]}`，输出工单数量。
