---
name: bad-binary
description: 坏包：夹带二进制可执行内容（扩展名伪装为 .md）。
metadata:
  ai-platform.capabilities: "ticket.search"
  ai-platform.risk-level: read
  ai-platform.data-class: public
  ai-platform.sandbox: none
---

# 二进制（应被 structure 拒绝）

## 何时使用
用户要求检索工单时。

## 步骤
1. 用 `ticket.search` 按关键字检索工单（只读）。
2. 按条目列出工单号与标题。
3. 参考 `references/blob.md`。
