"""Skill 包对象存储（D139）：``SKILL_STORE=fs|s3``。键形如 ``skills/<tenant>/<name>/<version>/{source,package}.tgz``。

- fs：``SKILL_FS_DIR`` 下按键存文件（开发 / 夜间 CI）；写入先落临时文件再原子 rename；
- s3：minio 客户端（compose-dev MinIO 或任何 S3 兼容存储），桶不存在时创建。
节点不直连存储，只经网关 ``GET /skills/{n}/versions/{v}/package`` 取包。
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Protocol

from capability_gateway import config

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class PackageStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...


def _check(key: str) -> str:
    if not _KEY.match(key) or ".." in key.split("/"):
        raise ValueError(f"invalid object key: {key!r}")
    return key


class FsStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _put(self, key: str, data: bytes) -> None:
        path = self.root / _check(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put, key, data)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread((self.root / _check(key)).read_bytes)


class S3Store:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, *, secure: bool) -> None:
        self._args = (endpoint, access_key, secret_key, secure)
        self._client: Any = None
        self.bucket = bucket
        self._ready = False

    @property
    def client(self) -> Any:
        """首次使用时创建 minio 客户端（导入期不建连接，也不在 prod 配置校验前因缺凭据报错）。"""
        if self._client is None:
            from minio import Minio  # 可选依赖（extras: skills）

            endpoint, access_key, secret_key, secure = self._args
            self._client = Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)
        return self._client

    def _put(self, key: str, data: bytes) -> None:
        key = _check(key)
        if not self._ready:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
            self._ready = True
        self.client.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type="application/gzip")

    def _get(self, key: str) -> bytes:
        key = _check(key)
        resp = self.client.get_object(self.bucket, key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release_conn()

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put, key, data)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get, key)


def make_store() -> PackageStore:
    if config.SKILL_STORE == "s3":
        return S3Store(
            config.SKILL_S3_ENDPOINT,
            config.SKILL_S3_ACCESS_KEY,
            config.SKILL_S3_SECRET_KEY,
            config.SKILL_S3_BUCKET,
            secure=config.SKILL_S3_SECURE,
        )
    if config.SKILL_STORE != "fs":
        raise RuntimeError(f"unknown SKILL_STORE: {config.SKILL_STORE}")
    return FsStore(config.SKILL_FS_DIR)


def key_for(tenant: str, name: str, version: str, kind: str) -> str:
    return f"skills/{tenant}/{name}/{version}/{kind}.tgz"
