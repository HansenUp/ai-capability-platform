"""F7 signed-user isolated control API. Only explicitly injected temporary ledger."""

import inspect
import json
import os
from typing import Literal
from uuid import UUID

import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from console_api import owner_auth
from console_api.auth import Role
from console_api.node_control_ledger import Ledger

router = APIRouter(prefix="/isolated-node-control", tags=["isolated-node-control"])


class Store(Ledger):
    def __init__(self):
        super().__init__()
        self.profile = owner_auth._profile
        self.admitted = False
        self.apply_node = None  # injected synchronous synthetic application transport only
        self.read_observation = None  # fixture-only natural drain journal observation
        self.read_evidence = None  # injected read-only node journal transport; no apply on recovery
        self.node_keys = {}  # fixture-enrolled node_id -> (issuer, kid, public RSA key)

    async def admit_fresh_nodes(self, session):
        from sqlalchemy import select

        from console_api.db import Node, NodeCommand

        self.admitted = False
        with self.transaction(write=False) as db:
            identifiers = [row[0] for row in db.execute("SELECT id FROM nodes").fetchall()]
        if not identifiers:
            raise HTTPException(409, "no synthetic nodes enrolled")
        # First isolated version accepts only new nodes: no old command, even a terminal one,
        # is sufficient proof that an old runtime has no application still in flight.
        for model, column in ((Node, Node.node_id), (NodeCommand, NodeCommand.node_id)):
            if (await session.execute(select(model).where(column.in_(identifiers)).limit(1))).first():
                raise HTTPException(409, "existing node or legacy command prevents isolated admission")
        self.admitted = True

    def verify_signed(self, attempt, token):
        with self.transaction(write=False) as db:
            op = self.read(db, "operations", attempt)
        return self.verify_node(op["node"], token)

    def verify_node(self, node_id, token):
        identity = self.node_keys.get(node_id)
        if identity is None or not isinstance(token, str) or len(token) > 16384:
            raise HTTPException(403, "node evidence identity unavailable")
        issuer, kid, key = identity
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or header.get("kid") != kid or any(x in header for x in ("jku", "jwk", "x5u", "crit")):
                raise ValueError
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=issuer,
                audience="f7-isolated-node-evidence",
                options={"require": ["iss", "aud", "sub", "iat", "nbf", "exp", "evidence"], "strict_aud": True},
            )
            if claims["sub"] != node_id or claims.get("identity_type") != "node" or any(x in claims for x in ("act", "may_act")):
                raise ValueError
            if any(type(claims[x]) is not int for x in ("iat", "nbf", "exp")) or not 0 < claims["exp"] - claims["iat"] <= 60:
                raise ValueError
            if not isinstance(claims["evidence"], dict):
                raise ValueError
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise HTTPException(403, "invalid signed node evidence") from exc
        return claims["evidence"]

    def reconcile_signed(self, attempt, token):
        return self.reconcile(attempt, self.verify_signed(attempt, token))

    def accept_boot_signed(self, node_id, token):
        return self.accept_boot(node_id, self.verify_node(node_id, token))

    def observe_signed(self, attempt, token):
        return self.observe_drained(attempt, self.verify_signed(attempt, token))


async def context(request):
    store = getattr(request.app.state, "isolated_node_control", None)
    if os.environ.get("CONSOLE_ENV", "dev") != "dev" or not isinstance(store, Store) or not store.admitted:
        raise HTTPException(503, "isolated node control disabled")
    profile, actor = await owner_auth.authenticate_async(request)
    if profile is not store.profile or actor.role != Role.PLATFORM_ADMIN:
        raise HTTPException(403, "bound signed platform administrator required")
    identity = json.dumps([profile.issuer, actor.tenant, actor.subject], separators=(",", ":"))
    return store, identity


class Preparation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    attempt: str = Field(min_length=36, max_length=36)
    node: str = Field(min_length=1, max_length=128)
    action: Literal["drain", "resume", "configure_limit"]
    expected: int = Field(ge=0)
    limit: int | None = Field(default=None, gt=0)


class Acknowledgement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    receipt: dict


@router.get("/operations")
async def recovery(request: Request):
    store, actor = await context(request)
    return {"synthetic": True, "operations": store.recover(actor)}


@router.post("/prepare")
async def prepare(body: Preparation, request: Request):
    store, actor = await context(request)
    try:
        if str(UUID(body.attempt)) != body.attempt:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(422, "canonical UUID required") from exc
    return store.prepare(actor, body.node, body.action, body.expected, body.attempt, body.limit)


@router.get("/operations/{attempt}")
async def receipt(attempt: UUID, request: Request):
    store, actor = await context(request)
    with store.transaction(write=False) as db:
        return store.owned(db, str(attempt), actor)


@router.post("/operations/{attempt}/execute")
async def execute(attempt: UUID, request: Request):
    store, actor = await context(request)
    if store.apply_node is None:
        raise HTTPException(503, "synthetic node adapter unavailable; preparation retained")
    op, claimed = store.claim(actor, str(attempt))
    if not claimed:
        return op
    # Re-validate immediately before the synthetic effect, including profile/role and scope.
    # No awaits occur between this check and the synchronous injected adapter.
    profile, current = owner_auth.authenticate(request, profile=store.profile)
    if current.role != Role.PLATFORM_ADMIN:
        raise HTTPException(403, "execution authorization revoked; read unresolved evidence")
    with store.transaction(write=False) as db:
        state = store.authorize(db, op["node"], actor)
    try:
        signed = store.apply_node(op, state)
        if inspect.isawaitable(signed):
            signed = await signed
    except Exception as exc:
        # Effect may have committed; retain EXECUTING and the node lock, never redispatch.
        raise HTTPException(503, "application outcome unknown; read original evidence") from exc
    result = store.reconcile_signed(op["id"], signed)
    # Persist effect evidence even if authorization changed while waiting, but do not expose it.
    current_store, current_actor = await context(request)
    if current_store is not store or current_actor != actor:
        raise HTTPException(403, "identity changed during application; reauthenticate for read recovery")
    return result


@router.post("/operations/{attempt}/withdraw")
async def withdraw(attempt: UUID, request: Request):
    store, actor = await context(request)
    return store.withdraw(actor, str(attempt))


@router.post("/operations/{attempt}/ack")
async def ack(attempt: UUID, body: Acknowledgement, request: Request):
    store, actor = await context(request)
    return store.ack(actor, str(attempt), body.receipt)


@router.post("/operations/{attempt}/reconcile")
async def reconcile(attempt: UUID, request: Request):
    store, actor = await context(request)
    with store.transaction(write=False) as db:
        op = store.owned(db, str(attempt), actor)
    if op["status"] != "EXECUTING":
        return op
    if store.read_evidence is None:
        raise HTTPException(503, "read-only node evidence adapter unavailable; keep lock")
    token = store.read_evidence(op["node"], str(attempt))
    if token is None:
        return op  # absence never terminates or unlocks
    return store.reconcile_signed(str(attempt), token)


@router.get("")
async def snapshot(request: Request):
    store, actor = await context(request)
    issuer, tenant, subject = json.loads(actor)
    return {"synthetic": True, "issuer": issuer, "tenant_id": tenant, "subject": subject, "nodes": store.snapshot(actor)}


@router.post("/operations/{attempt}/observe")
async def observe(attempt: UUID, request: Request):
    store, actor = await context(request)
    with store.transaction(write=False) as db:
        op = store.owned(db, str(attempt), actor)
    if store.read_observation is None:
        raise HTTPException(503, "read-only observation adapter unavailable")
    token = store.read_observation(op["node"], str(attempt))
    if token is None:
        return {"observed": False, "operation": op}
    store.observe_signed(str(attempt), token)
    return {"observed": True, "operation": op}
