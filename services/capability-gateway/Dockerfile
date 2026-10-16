# capability-gateway 服务镜像。构建上下文必须是仓库根目录（需要拷贝 packages/contract）：
#   docker build -f services/capability-gateway/Dockerfile -t ai-platform/capability-gateway:dev .

# ---- builder：普通（非可编辑）安装到独立前缀，便于整体拷贝到运行阶段 ----
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY packages/contract /build/packages/contract
COPY services/capability-gateway /build/services/capability-gateway
RUN pip install --prefix=/install /build/packages/contract /build/services/capability-gateway

# ---- runtime：仅包含安装产物，非 root 用户运行 ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=builder /install /usr/local
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /app && chown app:app /app   # /app 可写：Runtime 默认把 SQLite 与工作区放在当前目录
USER app
WORKDIR /app
EXPOSE 8082
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/healthz', timeout=3).status == 200 else 1)"
CMD ["uvicorn", "capability_gateway.main:app", "--host", "0.0.0.0", "--port", "8082"]
