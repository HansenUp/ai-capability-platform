"""Skill 包存储：fs 往返与键校验；设置 GATEWAY_TEST_S3=1 时对 compose-dev MinIO（SKILL_S3_*）跑同样的往返。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from s3_resource_testkit import owned_s3

from capability_gateway import config
from capability_gateway.skill_store import FsStore, S3Store, key_for


async def test_fs_store_roundtrip_and_key_validation(tmp_path: Path) -> None:
    store = FsStore(str(tmp_path))
    key = key_for("default", "demo", "1.0.0", "source-abc")
    assert key == "skills/default/demo/1.0.0/source-abc.tgz"
    await store.put(key, b"data")
    assert await store.get(key) == b"data"
    for bad in ("../x", "/abs", "skills/../../etc", "skills/a b"):
        with pytest.raises(ValueError):
            await store.put(bad, b"x")


async def test_s3_invalid_keys_have_no_client_or_bucket_side_effects(monkeypatch) -> None:
    from types import SimpleNamespace

    calls = []
    client = SimpleNamespace(
        bucket_exists=lambda bucket: calls.append("exists") or False,
        make_bucket=lambda bucket: calls.append("create"),
        put_object=lambda *args, **kwargs: calls.append("put"),
        get_object=lambda *args, **kwargs: calls.append("get"),
    )

    def access(store):
        calls.append("client")
        return client

    monkeypatch.setattr(S3Store, "client", property(access))
    for key in ("../escape", "/absolute", "skills/../../escape", "skills/invalid key"):
        store = S3Store("unused.invalid", "synthetic", "synthetic", "synthetic", secure=False)
        with pytest.raises(ValueError):
            await store.put(key, b"synthetic")
        with pytest.raises(ValueError):
            await store.get(key)
        assert calls == []
        assert store._ready is False


async def test_s3_substitute_roundtrip_and_read_failure_release_connection() -> None:
    from types import SimpleNamespace

    calls, objects = [], {}

    def put(bucket, key, stream, length, **kwargs):
        objects[key] = stream.read()
        assert len(objects[key]) == length and kwargs["content_type"] == "application/gzip"

    fail = False

    def get(bucket, key):
        def read():
            if fail:
                raise OSError("synthetic read failure")
            return objects[key]

        return SimpleNamespace(read=read, close=lambda: calls.append("close"), release_conn=lambda: calls.append("release"))

    store = S3Store("unused.invalid", "synthetic", "synthetic", "synthetic", secure=False)
    store._client = SimpleNamespace(
        bucket_exists=lambda bucket: False, make_bucket=lambda bucket: calls.append("create"), put_object=put, get_object=get
    )
    key = key_for("synthetic", "demo", "1", "package")
    await store.put(key, b"synthetic")
    await store.put(key, b"updated")
    assert await store.get(key) == b"updated"
    fail = True
    with pytest.raises(OSError, match="synthetic read failure"):
        await store.get(key)
    assert calls == ["create", "close", "release", "close", "release"]


@pytest.mark.skipif(os.environ.get("GATEWAY_TEST_S3") != "1", reason="GATEWAY_TEST_S3 not set（需要 compose-dev MinIO）")
async def test_s3_store_roundtrip() -> None:
    store = S3Store(
        config.SKILL_S3_ENDPOINT, config.SKILL_S3_ACCESS_KEY, config.SKILL_S3_SECRET_KEY, "unused", secure=config.SKILL_S3_SECURE
    )
    key = key_for("default", "demo", "1.0.0", "package-abc")
    with owned_s3(store):
        await store.put(key, b"pkg")
        assert await store.get(key) == b"pkg"
