from __future__ import annotations

import copy
import json as json_module
import re
from contextlib import asynccontextmanager

from resource_testkit import ResourceLedger

from capability_gateway.search import EsIndex


class OwnedEsIndex(EsIndex):
    def __init__(self, url, embedder):
        self.ledger = ResourceLedger("cg-test")
        self.uncertain: set[str] = set()
        self.closing = False
        super().__init__(url, embedder, alias=self.ledger.namespace + "-alias", prefix=self.ledger.namespace + "-index-")

    async def _inspect(self, name):
        response = await super()._req("GET", f"/{name}", ok=(404,))
        if response.status_code == 404:
            return None
        body = response.json()
        if set(body) != {name}:
            raise ValueError("unexpected index inspection scope")
        index = body[name]
        if index["mappings"].get("_meta", {}).get("test_owner") != self.ledger.namespace:
            raise ValueError("index owner marker mismatch")
        identity = index["settings"]["index"]["uuid"]
        if not isinstance(identity, str) or not identity:
            raise ValueError("missing index identity")
        return identity

    async def _owned(self, name):
        if name not in self.ledger.owned or await self._inspect(name) != self.ledger.owned[name]:
            raise ValueError("index ownership not established")

    async def _alias(self):
        response = await super()._req("GET", f"/_alias/{self.alias}", ok=(404,))
        if response.status_code == 200:
            for name in response.json():
                await self._owned(name)
        return response

    async def _req(self, method, path, json=None, content=None, ok=(), admin=False):
        if self.closing:
            raise ValueError("ES test scope is closed")
        target = path.split("?", 1)[0]
        if method == "GET" and target == f"/_alias/{self.alias}":
            return await self._alias()
        if method == "PUT" and target.count("/") == 1:
            name = target[1:]
            if (
                not name.startswith(self.prefix)
                or not re.fullmatch(r"[a-z0-9-]+", name)
                or name in self.ledger.owned
                or name in self.uncertain
            ):
                raise ValueError("index creation outside fresh namespace")
            probe = await super()._req("GET", target, ok=(404,))
            if probe.status_code != 404:
                raise ValueError("index creation collision")
            body = copy.deepcopy(json)
            body.setdefault("mappings", {}).setdefault("_meta", {})["test_owner"] = self.ledger.namespace
            self.uncertain.add(name)
            response = await super()._req(method, path, json=body, admin=admin)
            if response.json().get("acknowledged") is not True:
                raise ValueError("index creation not acknowledged")
            identity = await self._inspect(name)
            if identity is None:
                raise ValueError("created index not observable")
            self.ledger.created(name, identity)
            self.uncertain.remove(name)
            return response
        if method == "DELETE" and target.count("/") == 1:
            return await self._delete_owned(target[1:])
        if method == "POST" and target == "/_aliases":
            await self._alias()
            for action in json["actions"]:
                if len(action) != 1 or next(iter(action)) not in {"add", "remove"}:
                    raise ValueError("unexpected alias operation")
                details = next(iter(action.values()))
                if set(details) != {"index", "alias"} or details["alias"] != self.alias:
                    raise ValueError("foreign alias operation")
                await self._owned(details["index"])
        elif method == "POST" and target == "/_bulk":
            lines = content.decode().splitlines()
            if len(lines) % 2:
                raise ValueError("invalid bulk framing")
            for line in lines[::2]:
                metadata = json_module.loads(line)
                if set(metadata) != {"index"}:
                    raise ValueError("unexpected bulk operation")
                await self._owned(metadata["index"]["_index"])
        elif target.startswith(f"/{self.alias}/"):
            response = await self._alias()
            if response.status_code != 200:
                raise ValueError("test alias not established")
        else:
            raise ValueError("request outside owned test resources")
        return await super()._req(method, path, json=json, content=content, ok=ok, admin=admin)

    async def _delete_owned(self, name):
        await self._owned(name)
        response = await super()._req("DELETE", f"/{name}", admin=True)
        if response.json().get("acknowledged") is not True:
            raise ValueError("index deletion not acknowledged")
        del self.ledger.owned[name]
        return response

    async def cleanup(self):
        self.closing = True
        errors = []
        for name in tuple(self.ledger.owned):
            try:
                identity = await self._inspect(name)
                if identity is None:
                    del self.ledger.owned[name]
                else:
                    await self._delete_owned(name)
            except Exception as error:
                errors.append(error)
        if self.uncertain:
            errors.append(ValueError("unconfirmed index creation retained: " + ", ".join(sorted(self.uncertain))))
        if errors:
            raise ExceptionGroup("ES cleanup unresolved", errors)


@asynccontextmanager
async def owned_es(url, embedder):
    index = OwnedEsIndex(url, embedder)
    primary = None
    try:
        yield index
    except BaseException as error:
        primary = error
    try:
        await index.cleanup()
    except BaseException as error:
        if primary is not None:
            raise BaseExceptionGroup("test and ES cleanup failed", [primary, error]) from None
        raise
    if primary is not None:
        raise primary
