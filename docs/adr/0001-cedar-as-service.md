# ADR-0001 Cedar 判定服务化

- 状态：已采纳
- 日期：2026-09-11
- 关联需求：CG-17、RT-16

## 背景
授权判定需被能力网关与 Runtime 安全管控层共同调用，判定必须唯一。Cedar 的 Python 绑定成熟度不足。

## 决策
`services/policy` 以 HTTP 服务方式提供 `POST /authz/check`，内部调用 Cedar（Rust CLI / 库）；调用方只依赖 HTTP 契约。开发环境提供纯 Python 的最小规则评估器作为回退，行为以 Cedar 策略测试为准。

## 备选方案
OPA（Rego）：更通用，但应用授权表达不如 Cedar 直接；保留为替换路径。

## 后果
多语言可调用；策略版本化与单元测试集中在一处；增加一次网络调用，需缓存判定结果。
