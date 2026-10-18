# policy 服务镜像。构建上下文必须是仓库根目录（需要拷贝 packages/contract）：
#   docker build -f services/policy/Dockerfile -t ai-platform/policy:dev .
# Cedar 引擎 sidecar（Rust，从源码编译）是同一文件中的独立目标：
#   docker build -f services/policy/Dockerfile --target cedar-engine -t ai-platform/cedar-engine:dev .
# 只按需拷贝源码（不拷贝本地 cedar-engine/target 等构建产物）。

# ---- cedar-build：从源码编译 Cedar 引擎（依赖由 Cargo.lock 锁定，只从官方 crates.io 下载） ----
FROM rust:1.89-slim-bookworm AS cedar-build
WORKDIR /build/services/policy/cedar-engine
COPY services/policy/cedar-engine/Cargo.toml services/policy/cedar-engine/Cargo.lock ./
COPY services/policy/cedar-engine/src ./src
RUN cargo build --release --locked && strip target/release/cedar-engine

# ---- cedar-engine：sidecar 运行镜像（非 root；策略文件可挂载覆盖，引擎每 5 s 检查 mtime 热加载） ----
FROM debian:bookworm-slim AS cedar-engine
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=cedar-build /build/services/policy/cedar-engine/target/release/cedar-engine /usr/local/bin/cedar-engine
COPY services/policy/cedar/policies.cedar services/policy/cedar/schema.cedarschema /etc/cedar/
ENV CEDAR_LISTEN=0.0.0.0:8183 \
    CEDAR_POLICY_FILE=/etc/cedar/policies.cedar \
    CEDAR_SCHEMA_FILE=/etc/cedar/schema.cedarschema
USER app
EXPOSE 8183
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["bash", "-c", "exec 3<>/dev/tcp/127.0.0.1/8183"]
CMD ["cedar-engine"]

# ---- builder：普通（非可编辑）安装到独立前缀，便于整体拷贝到运行阶段 ----
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY packages/contract /build/packages/contract
COPY services/policy/pyproject.toml /build/services/policy/pyproject.toml
COPY services/policy/src /build/services/policy/src
RUN pip install --prefix=/install /build/packages/contract /build/services/policy

# ---- runtime（默认目标）：仅包含安装产物，非 root 用户运行 ----
# 默认 POLICY_ENGINE=cedar，经 CEDAR_URL 访问 cedar-engine；baseline 只能显式开启，POLICY_ENV=prod 时拒绝启动。
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=builder /install /usr/local
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /app && chown app:app /app   # /app 可写：Runtime 默认把 SQLite 与工作区放在当前目录
USER app
WORKDIR /app
EXPOSE 8083
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8083/healthz', timeout=3).status == 200 else 1)"
CMD ["uvicorn", "policy_service.main:app", "--host", "0.0.0.0", "--port", "8083"]
