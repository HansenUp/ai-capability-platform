from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from s3_resource_testkit import OwnedS3Resources, owned_s3

from capability_gateway.skill_store import S3Store


class FakeS3Error(OSError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class FakeS3:
    def __init__(self):
        self.buckets = {}
        self.calls = []
        self.counter = 0
        self.fail_put = None
        self.fail_remove = set()
        self.lose_bucket_response = False

    def bucket_exists(self, bucket):
        self.calls.append(("exists", bucket))
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.calls.append(("create", bucket))
        if bucket in self.buckets:
            raise FakeS3Error("BucketAlreadyOwnedByYou")
        self.buckets[bucket] = {}
        if self.lose_bucket_response:
            raise OSError("bucket response lost")

    def stat_object(self, bucket, key):
        self.calls.append(("stat", bucket, key))
        if key not in self.buckets[bucket]:
            raise FakeS3Error("NoSuchKey")
        return self.buckets[bucket][key][1]

    def put_object(self, bucket, key, stream, length, **kwargs):
        self.calls.append(("put", bucket, key))
        data = stream.read()
        assert len(data) == length
        self.counter += 1
        stat = SimpleNamespace(
            metadata={"x-amz-meta-" + name: value for name, value in kwargs["metadata"].items()},
            etag=str(self.counter),
            version_id=None,
        )
        self.buckets[bucket][key] = (data, stat)
        if key == self.fail_put:
            raise OSError("object response lost")
        return stat

    def get_object(self, bucket, key):
        self.calls.append(("get", bucket, key))
        return SimpleNamespace(read=lambda: self.buckets[bucket][key][0], close=lambda: None, release_conn=lambda: None)

    def remove_object(self, bucket, key, version_id=None):
        self.calls.append(("remove", bucket, key, version_id))
        if key in self.fail_remove:
            raise OSError("object cleanup failure")
        del self.buckets[bucket][key]

    def list_objects(self, bucket, recursive=False):
        self.calls.append(("list", bucket))
        assert recursive is True
        return [SimpleNamespace(object_name=key) for key in self.buckets[bucket]]

    def remove_bucket(self, bucket):
        self.calls.append(("remove_bucket", bucket))
        if self.buckets[bucket]:
            raise FakeS3Error("BucketNotEmpty")
        del self.buckets[bucket]


def make_store(fake):
    store = S3Store("unused.invalid", "synthetic", "synthetic", "unused", secure=False)
    store._client = fake
    return store


async def test_s3_original_store_roundtrip_uses_owned_exact_cleanup():
    fake = FakeS3()
    fake.buckets["foreign"] = {"do-not-touch": b"foreign"}
    store = make_store(fake)
    with owned_s3(store) as resources:
        await store.put("skills/synthetic/package", b"package")
        assert await store.get("skills/synthetic/package") == b"package"
        assert store.bucket == resources.bucket
    assert fake.buckets == {"foreign": {"do-not-touch": b"foreign"}}
    assert all(call[1] != "foreign" for call in fake.calls)


def test_s3_bucket_collision_is_refused_without_adoption_or_cleanup():
    fake = FakeS3()
    resources = OwnedS3Resources(make_store(fake))
    fake.buckets[resources.bucket] = {"foreign": b"original"}
    with pytest.raises(ValueError, match="collision"):
        resources.prepare()
    resources.cleanup()
    assert fake.calls == [("exists", resources.bucket)]
    assert resources.created is False


async def test_s3_object_collision_and_foreign_bucket_calls_are_refused():
    fake = FakeS3()
    store = make_store(fake)
    with owned_s3(store) as resources:
        await store.put("skills/package", b"first")
        with pytest.raises(ValueError, match="collision"):
            await store.put("skills/package", b"second")
        with pytest.raises(ValueError, match="outside"):
            resources.get_object("foreign", "skills/package")
        assert await store.get("skills/package") == b"first"
    assert fake.buckets == {}


def test_s3_bucket_response_loss_never_cleans_up_unconfirmed_bucket():
    fake = FakeS3()
    fake.lose_bucket_response = True
    with pytest.raises(BaseExceptionGroup) as result:
        with owned_s3(make_store(fake)):
            pytest.fail("must not enter")
    assert "bucket response lost" in str(result.value.exceptions[0])
    assert "unconfirmed bucket" in str(result.value.exceptions[1])
    assert len(fake.buckets) == 1
    assert all(not call[0].startswith("remove") for call in fake.calls)


async def test_s3_object_response_loss_keeps_unknown_data_and_marker():
    fake = FakeS3()
    fake.fail_put = "skills/package"
    store = make_store(fake)
    with pytest.raises(BaseExceptionGroup) as result:
        with owned_s3(store) as resources:
            await store.put("skills/package", b"unknown")
    assert "object response lost" in str(result.value.exceptions[0])
    assert resources.uncertain == {"skills/package"}
    assert set(fake.buckets[resources.bucket]) == {resources.marker, "skills/package"}
    assert all(not call[0].startswith("remove") for call in fake.calls)


async def test_s3_replaced_object_is_not_deleted_and_primary_error_survives():
    fake = FakeS3()
    store = make_store(fake)
    primary = AssertionError("business assertion")
    with pytest.raises(BaseExceptionGroup) as result:
        with owned_s3(store) as resources:
            await store.put("skills/package", b"original")
            fake.buckets[resources.bucket]["skills/package"][1].etag = "replacement"
            raise primary
    assert result.value.exceptions[0] is primary
    assert set(fake.buckets[resources.bucket]) == {resources.marker, "skills/package"}
    assert all(not call[0].startswith("remove") for call in fake.calls)


async def test_s3_partial_cleanup_keeps_failed_key_and_cleans_other_owned_key():
    fake = FakeS3()
    store = make_store(fake)
    with pytest.raises(ExceptionGroup, match="unresolved"):
        with owned_s3(store) as resources:
            await store.put("skills/failed", b"one")
            await store.put("skills/success", b"two")
            fake.fail_remove.add("skills/failed")
    assert set(fake.buckets[resources.bucket]) == {resources.marker, "skills/failed"}


async def test_s3_unexpected_objects_prevent_bucket_cleanup():
    fake = FakeS3()
    store = make_store(fake)
    with pytest.raises(ValueError, match="unexpected objects"):
        with owned_s3(store) as resources:
            await store.put("skills/owned", b"one")
            fake.buckets[resources.bucket]["foreign"] = (b"foreign", None)
    assert set(fake.buckets[resources.bucket]) == {resources.marker, "foreign"}
    assert all(call[0] != "remove_bucket" for call in fake.calls)


async def test_s3_independent_scopes_cannot_cleanup_each_other():
    fake = FakeS3()
    first, second = make_store(fake), make_store(fake)
    with owned_s3(first) as one:
        with owned_s3(second) as two:
            assert one.bucket != two.bucket
            await first.put("skills/package", b"one")
            await second.put("skills/package", b"two")
        assert set(fake.buckets) == {one.bucket}
        assert await first.get("skills/package") == b"one"
    assert fake.buckets == {}


def test_s3_atomic_bucket_collision_after_preflight_is_not_adopted(monkeypatch):
    fake = FakeS3()
    resources = OwnedS3Resources(make_store(fake))
    fake.buckets[resources.bucket] = {"foreign": b"original"}
    monkeypatch.setattr(fake, "bucket_exists", lambda bucket: False)
    with pytest.raises(FakeS3Error, match="BucketAlreadyOwnedByYou"):
        resources.prepare()
    resources.cleanup()
    assert resources.created is False and resources.bucket_uncertain is False
    assert set(fake.buckets[resources.bucket]) == {"foreign"}
    assert all(not call[0].startswith("remove") for call in fake.calls)


async def test_s3_missing_or_replaced_marker_prevents_any_cleanup():
    for missing in (False, True):
        fake = FakeS3()
        store = make_store(fake)
        with pytest.raises(ValueError, match="owner"):
            with owned_s3(store) as resources:
                await store.put("skills/package", b"original")
                if missing:
                    del fake.buckets[resources.bucket][resources.marker]
                else:
                    fake.buckets[resources.bucket][resources.marker][1].metadata = {"x-amz-meta-test-owner": "foreign"}
        assert "skills/package" in fake.buckets[resources.bucket]
        assert all(not call[0].startswith("remove") for call in fake.calls)


async def test_s3_cleanup_uses_exact_observed_version_id():
    fake = FakeS3()
    store = make_store(fake)
    with owned_s3(store) as resources:
        await store.put("skills/package", b"original")
        fake.buckets[resources.bucket]["skills/package"][1].version_id = "observed-version"
    assert ("remove", resources.bucket, "skills/package", "observed-version") in fake.calls


async def test_original_s3_integration_body_uses_owned_lifecycle_with_fake_client(monkeypatch):
    import test_skill_store

    fake = FakeS3()
    monkeypatch.setattr(test_skill_store, "S3Store", lambda *args, **kwargs: make_store(fake))
    await test_skill_store.test_s3_store_roundtrip()
    assert fake.buckets == {}
    assert [call[0] for call in fake.calls].count("create") == 1
    assert [call[0] for call in fake.calls].count("remove_bucket") == 1


async def test_s3_concurrent_same_key_refuses_second_write_and_closed_scope_refuses_late_write():
    fake = FakeS3()
    store = make_store(fake)
    with owned_s3(store):
        results = await asyncio.gather(store.put("skills/package", b"one"), store.put("skills/package", b"two"), return_exceptions=True)
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert sum(call[0] == "put" and call[2] == "skills/package" for call in fake.calls) == 1
    calls = list(fake.calls)
    with pytest.raises(ValueError, match="active"):
        await store.put("skills/late", b"late")
    assert fake.calls == calls and fake.buckets == {}
