from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

import jwt

from console_api import owner_auth
from console_api.auth import Role
from console_api.owner_auth import Binding, LocalProfile


def identifier(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("Invalid Owner identifier")
    return value


def document(raw: str, fields: set[str], optional: frozenset[str] = frozenset()) -> dict:
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError("Duplicate Owner configuration field")
            result[name] = value
        return result

    if len(raw.encode()) > 2_000_000:
        raise ValueError("Owner configuration exceeds limit")
    body = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(body, dict) or not fields <= set(body) or set(body) - fields - optional:
        raise ValueError("Invalid Owner configuration fields")
    return body


def load(profile_json: str, bindings_json: str, public_jwk: str) -> tuple[int, LocalProfile]:
    config = document(profile_json, {"profile_id", "issuer", "audience", "client", "role"})
    for field in ("profile_id", "audience", "client", "role"):
        identifier(config[field])
    issuer = config["issuer"]
    if not isinstance(issuer, str) or not 1 <= len(issuer) <= 2048 or re.search(r"[\s\x00-\x1f\x7f]", issuer):
        raise ValueError("Invalid Owner issuer")
    parsed = urlsplit(issuer)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Owner issuer must be a fixed HTTPS identity")
    body = document(bindings_json, {"schema_version", "revision", "profile_id", "change_ref", "bindings"})
    if type(body["schema_version"]) is not int or body["schema_version"] != 1:
        raise ValueError("Invalid Owner schema version")
    revision = body["revision"]
    if type(revision) is not int or revision < 1 or body["profile_id"] != config["profile_id"]:
        raise ValueError("Invalid Owner binding revision")
    identifier(body["change_ref"])
    if not isinstance(body["bindings"], list) or len(body["bindings"]) > 2000:
        raise ValueError("Invalid Owner binding count")
    bindings = []
    identities, users = set(), set()
    for row in body["bindings"]:
        if not isinstance(row, dict) or set(row) != {"issuer", "tenant", "subject", "legacy_user"} or row["issuer"] != issuer:
            raise ValueError("Invalid Owner binding fields")
        tenant, subject, user = (identifier(row[name]) for name in ("tenant", "subject", "legacy_user"))
        identity = (issuer, tenant, subject)
        legacy = (tenant, user)
        if identity in identities or legacy in users:
            raise ValueError("Ambiguous Owner binding")
        identities.add(identity)
        users.add(legacy)
        bindings.append(Binding(issuer, tenant, subject, user))
    key = document(public_jwk, {"kty", "n", "e", "kid"}, frozenset({"key_ops"}))
    if key["kty"] != "RSA" or not identifier(key["kid"]) or key.get("key_ops", ["verify"]) != ["verify"]:
        raise ValueError("Invalid Owner public key")
    if jwt.PyJWK.from_dict(key, algorithm="RS256").key.key_size < 2048:
        raise ValueError("Owner RSA key too small")
    return revision, LocalProfile(
        issuer, config["audience"], config["client"], public_jwk, tuple(bindings), ((config["role"], Role.ADMIN),), True
    )


class IsolatedProfileStore:
    def __init__(self) -> None:
        self.revision = 0
        self.profile: LocalProfile | None = None

    def replace(self, profile_json: str, bindings_json: str, public_jwk: str) -> LocalProfile:
        self.profile = None
        revision, profile = load(profile_json, bindings_json, public_jwk)
        if revision <= self.revision:
            raise ValueError("Owner revision must increase")
        self.revision = revision
        self.profile = profile
        return profile

    def install_isolated(self, profile_json: str, bindings_json: str, public_jwk: str) -> LocalProfile:
        if os.environ.get("CONSOLE_ENV", "dev") != "dev" or os.environ.get("CONSOLE_OWNER_LOCAL_READ") != "1":
            raise ValueError("Owner isolated profile is disabled")
        owner_auth._profile = None
        profile = self.replace(profile_json, bindings_json, public_jwk)
        owner_auth._profile = profile
        return profile
