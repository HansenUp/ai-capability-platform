from __future__ import annotations

import io
import threading
from contextlib import contextmanager

from resource_testkit import ResourceLedger, preserve_errors


class OwnedS3Resources:
    def __init__(self, store):
        self.ledger = ResourceLedger("ai-skills-test")
        self.bucket = self.ledger.namespace + "-bucket"
        self.marker = "test-owner"
        self.raw = store.client
        self.store = store
        self.store.bucket = self.bucket
        self.created = False
        self.bucket_uncertain = False
        self.objects = {}
        self.uncertain = set()
        self.lock = threading.RLock()
        self.closing = False

    def _stat(self, key):
        try:
            return self.raw.stat_object(self.bucket, key)
        except Exception as error:
            if getattr(error, "code", None) == "NoSuchKey":
                return None
            raise

    def _check(self, key):
        if key not in self.objects:
            raise ValueError("object ownership not established")
        stat = self._stat(key)
        if stat is None:
            return None
        metadata = {name.lower(): value for name, value in stat.metadata.items()}
        if metadata.get("x-amz-meta-test-owner") != self.ledger.namespace or stat.etag != self.objects[key]:
            raise ValueError("object ownership changed")
        return stat

    def _owner(self):
        if not self.created or self._check(self.marker) is None:
            raise ValueError("bucket owner marker missing")

    def prepare(self):
        if self.closing or self.created or self.bucket_uncertain:
            raise ValueError("S3 test scope is not fresh")
        if self.raw.bucket_exists(self.bucket):
            raise ValueError("test bucket collision")
        self.bucket_uncertain = True
        try:
            self.raw.make_bucket(self.bucket)
        except Exception as error:
            if getattr(error, "code", None) in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                self.bucket_uncertain = False
            raise
        self.bucket_uncertain = False
        self.created = True
        self._put(self.marker, io.BytesIO(self.ledger.namespace.encode()), len(self.ledger.namespace))
        self.store._client = self
        self.store._ready = True

    def _put(self, key, stream, length, **kwargs):
        if self._stat(key) is not None or key in self.uncertain:
            raise ValueError("test object collision")
        self.uncertain.add(key)
        kwargs["metadata"] = {"test-owner": self.ledger.namespace}
        result = self.raw.put_object(self.bucket, key, stream, length, **kwargs)
        if not result.etag:
            raise ValueError("object write not acknowledged")
        self.objects[key] = result.etag
        self.uncertain.remove(key)
        return result

    def put_object(self, bucket, key, stream, length, **kwargs):
        with self.lock:
            if self.closing or bucket != self.bucket or key == self.marker:
                raise ValueError("write outside active test bucket")
            self._owner()
            return self._put(key, stream, length, **kwargs)

    def get_object(self, bucket, key):
        with self.lock:
            if self.closing or bucket != self.bucket:
                raise ValueError("read outside active test bucket")
            self._owner()
            if self._check(key) is None:
                raise ValueError("owned object missing")
            return self.raw.get_object(bucket, key)

    def cleanup(self):
        with self.lock:
            self.closing = True
            self._cleanup()

    def _cleanup(self):
        if self.bucket_uncertain:
            raise ValueError("unconfirmed bucket creation retained: " + self.bucket)
        if not self.created:
            return
        self._owner()
        errors = []
        for key in tuple(self.objects):
            if key == self.marker:
                continue
            try:
                stat = self._check(key)
                if stat is not None:
                    self.raw.remove_object(self.bucket, key, version_id=stat.version_id)
                del self.objects[key]
            except Exception as error:
                errors.append(error)
        if self.uncertain:
            errors.append(ValueError("unconfirmed object writes retained"))
        if errors:
            raise ExceptionGroup("S3 cleanup unresolved", errors)
        names = {item.object_name for item in self.raw.list_objects(self.bucket, recursive=True)}
        if names != {self.marker}:
            raise ValueError("unexpected objects retained; bucket not removed")
        marker = self._check(self.marker)
        if marker is None:
            raise ValueError("bucket owner marker disappeared")
        self.raw.remove_object(self.bucket, self.marker, version_id=marker.version_id)
        del self.objects[self.marker]
        self.raw.remove_bucket(self.bucket)
        self.created = False


@contextmanager
def owned_s3(store):
    resources = OwnedS3Resources(store)
    primary = None
    try:
        resources.prepare()
        yield resources
    except BaseException as error:
        primary = error
    preserve_errors(primary, resources.cleanup)
