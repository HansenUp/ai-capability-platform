# Model Gateway（LiteLLM Proxy）

统一模型出口：OpenAI / Anthropic 兼容端点、虚拟 Key、预算与配额、Fallback、Prompt Cache 透传、版本别名。

- `config.yaml`：模型别名规范 `<family>-<yyyy-mm>`（ADR-0006）；Agent 发布快照锁定别名，模型升级 = 新增别名 + 灰度。
- 启动：`docker compose -f deploy/compose-dev/docker-compose.yml up litellm`（端口 4000）。
- 对账：`reconcile.py`（S3 工作包）每日拉取 spend 表按部门 / Agent 出账。
