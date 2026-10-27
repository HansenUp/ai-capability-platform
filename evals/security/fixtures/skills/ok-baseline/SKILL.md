---
name: ok-baseline
description: 对照组：只读检索工单并列出结果，不含任何违规内容。
metadata:
  ai-platform.capabilities: "ticket.search"
  ai-platform.risk-level: read
  ai-platform.data-class: public
  ai-platform.sandbox: none
---

# 对照组（应通过扫描）

## 何时使用
用户要求检索工单时。

## 步骤
1. 用 `ticket.search` 按关键字检索工单（只读）。
2. 按条目列出工单号与标题。
