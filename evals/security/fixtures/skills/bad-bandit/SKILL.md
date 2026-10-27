---
name: bad-bandit
description: 坏包：脚本含 bandit HIGH 级问题（弱哈希用于安全用途）。
metadata:
  ai-platform.capabilities: "ticket.search"
  ai-platform.risk-level: read
  ai-platform.data-class: public
  ai-platform.sandbox: read-only
---

# bandit HIGH（应被 bandit 规则拒绝）

## 何时使用
用户要求检索工单时。

## 步骤
1. 用 `ticket.search` 按关键字检索工单（只读）。
2. 按条目列出工单号与标题。
3. 运行 `scripts/digest.py` 为工单生成签名摘要。
