# runtime 服务镜像。构建上下文必须是仓库根目录（需要拷贝 packages/contract）：
#   docker build -f services/runtime/Dockerfile -t ai-platform/runtime:dev .

# ---- builder：普通（非可编辑）安装到独立前缀，便于整体拷贝到运行阶段 ----
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY packages/contract /build/packages/contract
COPY services/runtime /build/services/runtime
RUN pip install --prefix=/install /build/packages/contract "/build/services/runtime[mysql]"   # mysql：asyncmy 驱动

# ---- runtime：仅包含安装产物，非 root 用户运行 ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=builder /install /usr/local
# Alembic 迁移脚本不在 Python 包内：单独拷贝，并通过 RUNTIME_MIGRATIONS_DIR 告知 init_db（启动时自动 upgrade head）
COPY --from=builder /build/services/runtime/migrations /opt/runtime/migrations
COPY --from=builder /build/services/runtime/alembic.ini /opt/runtime/alembic.ini
ENV RUNTIME_MIGRATIONS_DIR=/opt/runtime/migrations
# 数据分级矩阵（S9 OPT-4）：镜像内默认随附开发矩阵（未签署）；生产以 DATA_CLASS_MATRIX_FILE 指向挂载的已签署矩阵
COPY --from=builder /build/services/runtime/data_class_matrix.dev.json /opt/runtime/data_class_matrix.dev.json
ENV DATA_CLASS_MATRIX_FILE=/opt/runtime/data_class_matrix.dev.json
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /app && chown app:app /app   # /app 可写：Runtime 默认把 SQLite 与工作区放在当前目录
USER app
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"
CMD ["uvicorn", "runtime.main:app", "--host", "0.0.0.0", "--port", "8080"]
