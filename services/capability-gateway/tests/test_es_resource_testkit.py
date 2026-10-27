from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from es_resource_testkit import OwnedEsIndex, owned_es

from capability_gateway import config, search
from capability_gateway.embed import get_embedder


class FakeEs:
    def __init__(self):
        self.indices = {}
        self.aliases = {}
        self.requests = []
        self.fail_create = False
        self.fail_delete = False

    def __call__(self, request):
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if path.startswith("/_alias/"):
            alias = path.rsplit("/", 1)[1]
            names = self.aliases.get(alias)
            return httpx.Response(200, json={name: {} for name in names}) if names else httpx.Response(404)
        if path == "/_aliases":
            for action in json.loads(request.content)["actions"]:
                operation, details = next(iter(action.items()))
                names = self.aliases.setdefault(details["alias"], set())
                if operation == "add":
                    names.add(details["index"])
                else:
                    names.remove(details["index"])
            return httpx.Response(200, json={"acknowledged": True})
        if path == "/_bulk":
            return httpx.Response(200, json={"errors": False})
        name = path[1:]
        if method == "GET":
            return httpx.Response(200, json={name: self.indices[name]}) if name in self.indices else httpx.Response(404)
        if method == "PUT":
            if name in self.indices:
                return httpx.Response(400, json={"error": "already exists"})
            body = json.loads(request.content)
            self.indices[name] = {"mappings": body["mappings"], "settings": {"index": {"uuid": uuid4().hex}}}
            if self.fail_create:
                raise httpx.ReadError("creation response lost", request=request)
            return httpx.Response(200, json={"acknowledged": True})
        if method == "DELETE":
            if self.fail_delete:
                return httpx.Response(500, json={"error": "delete failure"})
            del self.indices[name]
            for names in self.aliases.values():
                names.discard(name)
            return httpx.Response(200, json={"acknowledged": True})
        raise AssertionError((method, path))


@pytest.fixture
def fake_es(monkeypatch):
    fake = FakeEs()
    monkeypatch.setattr(config, "transport", httpx.MockTransport(fake))
    return fake


async def test_es_ensure_reindex_and_cleanup_use_exact_owned_names(fake_es):
    fake_es.indices["foreign"] = {"untouched": True}
    async with owned_es("http://127.0.0.1:9200", get_embedder("hash")) as index:
        await index.ensure()
        await index.ensure()
        original = set(index.ledger.owned)
        name = await index.reindex([])
        assert set(index.ledger.owned) == {name}
        assert original.isdisjoint(fake_es.indices)
        assert fake_es.aliases[index.alias] == {name}
    assert fake_es.indices == {"foreign": {"untouched": True}}
    deleted = [path[1:] for method, path in fake_es.requests if method == "DELETE"]
    assert set(deleted) == original | {name}
    assert all("*" not in path for _, path in fake_es.requests)


async def test_es_creation_and_alias_collisions_are_refused_without_deletion(fake_es):
    index = OwnedEsIndex("http://127.0.0.1:9200", get_embedder("hash"))
    name = search.index_base(index.embedder, index.prefix)
    fake_es.indices[name] = {"foreign": True}
    with pytest.raises(ValueError, match="collision"):
        await index.ensure()
    await index.cleanup()
    index = OwnedEsIndex("http://127.0.0.1:9200", get_embedder("hash"))
    fake_es.aliases[index.alias] = {name}
    with pytest.raises(ValueError, match="ownership"):
        await index.ensure()
    assert index.ledger.owned == {}
    assert all(method != "DELETE" for method, _ in fake_es.requests)


async def test_es_lost_creation_response_is_retained_not_adopted(fake_es):
    fake_es.fail_create = True
    with pytest.raises(BaseExceptionGroup) as result:
        async with owned_es("http://127.0.0.1:9200", get_embedder("hash")) as index:
            await index.ensure()
    assert isinstance(result.value.exceptions[0], search.SearchUnavailable)
    assert "ES cleanup unresolved" in str(result.value.exceptions[1])
    assert len(fake_es.indices) == 1 and index.ledger.owned == {} and len(index.uncertain) == 1
    assert all(method != "DELETE" for method, _ in fake_es.requests)


async def test_es_replaced_identity_prevents_cleanup_and_keeps_primary(fake_es):
    primary = AssertionError("test failed")
    with pytest.raises(BaseExceptionGroup) as result:
        async with owned_es("http://127.0.0.1:9200", get_embedder("hash")) as index:
            await index.ensure()
            name = next(iter(index.ledger.owned))
            fake_es.indices[name]["settings"]["index"]["uuid"] = "replacement"
            raise primary
    assert result.value.exceptions[0] is primary
    assert name in fake_es.indices and name in index.ledger.owned
    assert all(method != "DELETE" for method, _ in fake_es.requests)


async def test_es_unowned_bulk_and_alias_actions_never_reach_transport(fake_es):
    index = OwnedEsIndex("http://127.0.0.1:9200", get_embedder("hash"))
    await index.ensure()
    for path, kwargs in [
        ("/_bulk", {"content": b'{"index":{"_index":"foreign"}}\n{}\n'}),
        ("/_aliases", {"json": {"actions": [{"add": {"index": "foreign", "alias": index.alias}}]}}),
    ]:
        before = len(fake_es.requests)
        with pytest.raises(ValueError):
            await index._req("POST", path, **kwargs)
        assert all(method == "GET" for method, _ in fake_es.requests[before:])
    await index.cleanup()


async def test_es_independent_scopes_do_not_clean_up_each_other(fake_es):
    first, second = [OwnedEsIndex("http://127.0.0.1:9200", get_embedder("hash")) for _ in range(2)]
    await asyncio.gather(first.ensure(), second.ensure())
    await first.cleanup()
    assert set(fake_es.indices) == set(second.ledger.owned)
    await second.cleanup()
    assert fake_es.indices == {}


async def test_es_delete_failure_is_reported_and_not_forgotten(fake_es):
    index = OwnedEsIndex("http://127.0.0.1:9200", get_embedder("hash"))
    await index.ensure()
    fake_es.fail_delete = True
    with pytest.raises(ExceptionGroup, match="unresolved"):
        await index.cleanup()
    assert set(index.ledger.owned) == set(fake_es.indices)


async def test_es_bulk_success_and_owner_marker_replacement(fake_es):
    index = OwnedEsIndex("http://127.0.0.1:9200", get_embedder("hash"))
    await index.ensure()
    name = await index.reindex([{"id": "synthetic", "row_version": 1}])
    assert ("POST", "/_bulk") in fake_es.requests
    fake_es.indices[name]["mappings"]["_meta"]["test_owner"] = "foreign"
    with pytest.raises(ExceptionGroup):
        await index.cleanup()
    assert name in fake_es.indices


async def test_es_atomic_create_collision_is_rejected_even_when_base_client_accepts_400(fake_es, monkeypatch):
    index = OwnedEsIndex("http://127.0.0.1:9200", get_embedder("hash"))
    name = search.index_base(index.embedder, index.prefix)
    original = fake_es.__call__

    def race(request):
        if request.method == "PUT":
            fake_es.indices[name] = {"foreign": True}
        return original(request)

    monkeypatch.setattr(config, "transport", httpx.MockTransport(race))
    with pytest.raises(search.SearchUnavailable):
        await index.ensure()
    assert index.ledger.owned == {} and name in index.uncertain
    with pytest.raises(ExceptionGroup):
        await index.cleanup()
    assert fake_es.indices[name] == {"foreign": True}
    assert all(method != "DELETE" for method, _ in fake_es.requests)


async def test_original_es_fixture_uses_owned_lifecycle_with_fake_transport(fake_es, monkeypatch):
    import test_es_live

    monkeypatch.setattr(test_es_live, "ES", "http://127.0.0.1:9200")
    fixture = test_es_live.es.__wrapped__()
    index = await anext(fixture)
    try:
        assert isinstance(index, OwnedEsIndex)
        await index.ensure()
        await index.reindex([])
    finally:
        await fixture.aclose()
    assert fake_es.indices == {}
    calls = list(fake_es.requests)
    with pytest.raises(ValueError, match="closed"):
        await index.ensure()
    assert fake_es.requests == calls
