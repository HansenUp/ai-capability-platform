"""网关测试替身：一个 httpx.MockTransport 同时扮演 Keycloak（JWKS + 令牌端点）、策略服务、REST 连接器与 Elasticsearch。"""

from __future__ import annotations

import json
import math
import time
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from capability_gateway.embed import char_bigrams

ISSUER = "http://idp.test/realms/ai"
TOKEN_URL = f"{ISSUER}/protocol/openid-connect/token"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
CONNECTOR_URL = "http://ticket.test"
ES_URL = "http://es.test"

TICKET_MANIFEST: list[dict[str, Any]] = [
    {"name": "ticket.search", "risk_level": "read", "input_schema": {"type": "object"}},
    {"name": "ticket.get", "risk_level": "read", "input_schema": {"type": "object"}},
    {
        "name": "ticket.close_batch",
        "risk_level": "write_high",
        "supports_dry_run": True,
        "requires_idempotency_key": True,
        "compensate_tool": "ticket.reopen_batch",
        "input_schema": {"type": "object"},
    },
    {"name": "ticket.reopen_batch", "risk_level": "write_high", "supports_dry_run": True, "requires_idempotency_key": True},
]


class Idp:
    """签发测试令牌的 IdP（ES256）。"""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.kid = "idp-1"

    def jwks(self) -> dict[str, Any]:
        jwk = ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [jwk | {"kid": self.kid, "use": "sig", "alg": "ES256"}]}

    def token(self, **claims: Any) -> str:
        now = int(time.time())
        body = {"iss": ISSUER, "iat": now, "exp": now + 300} | claims
        return jwt.encode(body, self.key, algorithm="ES256", headers={"kid": self.kid})

    def service_token(self, azp: str = "ai-runtime-svc", **claims: Any) -> str:
        return self.token(**({"sub": f"service-account-{azp}", "azp": azp, "aud": "capability-gateway"} | claims))

    def user_token(self, sub: str = "user-1", tenant: str | None = "default", **claims: Any) -> str:
        extra = {"tenant": tenant} if tenant is not None else {}
        return self.token(
            **({"sub": sub, "azp": "portal-web", "aud": "ai-runtime", "realm_access": {"roles": ["employee"]}} | extra | claims)
        )


def _match(doc: dict[str, Any], f: dict[str, Any]) -> bool:
    def val(field: str) -> Any:
        return doc.get(field.removesuffix(".keyword"))

    def eq(field: str, v: Any) -> bool:
        dv = val(field)
        return v in dv if isinstance(dv, list) else dv == v

    if "term" in f:
        ((k, v),) = f["term"].items()
        return eq(k, v)
    if "terms" in f:
        ((k, vs),) = f["terms"].items()
        return any(eq(k, v) for v in vs)
    if "bool" in f:
        should = f["bool"].get("should", [])
        return sum(_match(doc, s) for s in should) >= f["bool"].get("minimum_should_match", 1)
    raise AssertionError(f"unsupported filter {f}")


class FakeES:
    """内存版 ES：别名、索引、external_gte 版本写入、bool/term/terms 过滤、multi_match（字 2-gram 计数）与 kNN（余弦）。"""

    def __init__(self) -> None:
        self.indices: dict[str, dict[str, dict[str, Any]]] = {}
        self.aliases: dict[str, set[str]] = {}
        self.searches: list[dict[str, Any]] = []
        self.down = False

    def _resolve(self, name: str) -> str:
        idx = self.aliases.get(name)
        if idx:
            assert len(idx) == 1
            return next(iter(idx))
        return name

    def _put(self, index: str, doc_id: str, doc: dict[str, Any], version: int | None) -> int:
        docs = self.indices[index]
        old = docs.get(doc_id)
        if old is not None and version is not None and old["row_version"] > version:
            return 409
        docs[doc_id] = doc
        return 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("es down", request=request)
        parts = [p for p in request.url.path.split("/") if p]
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        m = request.method
        if parts[0] == "_alias":
            alias = parts[1]
            if alias not in self.aliases or not self.aliases[alias]:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={i: {"aliases": {alias: {}}} for i in self.aliases[alias]})
        if parts == ["_aliases"]:
            for a in json.loads(request.content)["actions"]:
                ((op, spec),) = a.items()
                s = self.aliases.setdefault(spec["alias"], set())
                (s.add if op == "add" else s.discard)(spec["index"])
            return httpx.Response(200, json={"acknowledged": True})
        if parts == ["_bulk"]:
            lines = [json.loads(x) for x in request.content.decode().splitlines() if x.strip()]
            for meta, doc in zip(lines[::2], lines[1::2], strict=True):
                spec = meta["index"]
                self._put(spec["_index"], spec["_id"], doc, spec.get("version"))
            return httpx.Response(200, json={"errors": False})
        if len(parts) == 1:
            if m == "PUT":
                if parts[0] in self.indices:
                    return httpx.Response(400, json={"error": "exists"})
                self.indices[parts[0]] = {}
                return httpx.Response(200, json={"acknowledged": True})
            if m == "DELETE":
                self.indices.pop(parts[0], None)
                return httpx.Response(200, json={"acknowledged": True})
        index = self._resolve(parts[0])
        if index not in self.indices:
            return httpx.Response(404, json={"error": "no such index"})
        if parts[1] == "_doc":
            if m == "PUT":
                return httpx.Response(self._put(index, parts[2], json.loads(request.content), int(q["version"])), json={})
            self.indices[index].pop(parts[2], None)
            return httpx.Response(200, json={})
        if parts[1] == "_search":
            body = json.loads(request.content)
            self.searches.append(body)
            docs = self.indices[index].values()
            if "knn" in body:
                knn = body["knn"]
                cands = [d for d in docs if all(_match(d, f) for f in knn["filter"])]
                qv = knn["query_vector"]

                def cos(v: list[float]) -> float:
                    dot = sum(a * b for a, b in zip(qv, v, strict=True))
                    return dot / (math.sqrt(sum(a * a for a in qv)) * math.sqrt(sum(b * b for b in v)))

                hits = sorted(cands, key=lambda d: (-cos(d["embedding"]), d["id"]))[: knn["k"]]
            else:
                b = body["query"]["bool"]
                cands = [d for d in docs if all(_match(d, f) for f in b["filter"])]
                grams = set(char_bigrams(b["must"]["multi_match"]["query"]))

                def score(d: dict[str, Any]) -> int:
                    text = f"{d['name']} {d['description']}"
                    return sum(1 for g in char_bigrams(text) if g in grams)

                hits = sorted((d for d in cands if score(d) > 0), key=lambda d: (-score(d), d["id"]))[: body["size"]]
            return httpx.Response(200, json={"hits": {"hits": [{"_id": d["id"]} for d in hits]}})
        raise AssertionError(f"unexpected ES request {m} {request.url}")


class Upstream:
    """替身：IdP、策略服务、REST 连接器（ticket.test）、ES（es.test）。"""

    def __init__(self) -> None:
        self.idp = Idp()
        self.es = FakeES()
        self.token_calls: list[dict[str, str]] = []
        self.policy: list[dict[str, Any]] = []  # 每条判定请求（展开批量）
        self.policy_batches: list[int] = []
        self.decisions: dict[str, str] = {}  # 能力 id → 判定，缺省 allow；require_approval 在 context.approved=true 时为 allow
        self.baseline = False  # True：按策略服务的 baseline 规则（与 Cedar 同一套）判定，忽略 decisions
        self.policy_down = False
        self.connector: list[httpx.Request] = []
        self.connector_down = False
        self.connector_status = 200
        self.manifest: list[dict[str, Any]] = [dict(t) for t in TICKET_MANIFEST]
        self.obo_expires_in = 300
        self.obo_count = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "idp.test":
            if path.endswith("/certs"):
                return httpx.Response(200, json=self.idp.jwks())
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.token_calls.append(form)
            self.obo_count += 1
            return httpx.Response(200, json={"access_token": f"obo-{self.obo_count}", "expires_in": self.obo_expires_in})
        if host == "policy.test":
            if self.policy_down:
                raise httpx.ConnectError("policy down", request=request)
            reqs = json.loads(request.content)["requests"]
            self.policy += reqs
            self.policy_batches.append(len(reqs))
            results = [self._decide(r) for r in reqs]
            return httpx.Response(200, json={"results": results, "engine": "cedar", "policy_version": "v1"})
        if host == "ticket.test":
            if self.connector_down:
                raise httpx.ConnectError("connector down", request=request)
            if path == "/healthz":
                return httpx.Response(200, json={"status": "ok"})
            if path == "/tools" and request.method == "GET":
                return httpx.Response(200, json=self.manifest)
            self.connector.append(request)
            if self.connector_status != 200:
                return httpx.Response(self.connector_status, json={"detail": "ticket not found or no permission"})
            return httpx.Response(200, json={"id": "T-1001", "title": "x"})
        if host == "es.test":
            return self.es(request)
        raise httpx.ConnectError(f"no route to {request.url}", request=request)  # 其他地址（如种子连接器）一律不可达

    def _decide(self, r: dict[str, Any]) -> dict[str, Any]:
        if self.baseline:
            from policy_service.baseline import authorize, synthesize

            ctx = r["context"]
            q1 = authorize(r["principal"], r["action"], r["resource"], ctx)
            q2 = None if ctx.get("approved") else authorize(r["principal"], r["action"], r["resource"], ctx | {"approved": True})
            decision, ids, reason = synthesize(q1, q2)
            return {"decision": decision, "policy_ids": ids, "reason": reason}
        decision = self.decisions.get(r["resource"]["id"], "allow")
        if decision == "require_approval" and r["context"].get("approved") is True:
            decision = "allow"
        return {"decision": decision, "policy_ids": ["p1"], "reason": None}
