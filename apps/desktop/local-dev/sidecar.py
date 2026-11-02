"""Local-development Runtime bridge: real Keycloak/JWT/SQLite, no business workers.

Only the fixed native parent may supply an empty owned directory and pipe commands.
The HTTP socket serves an OAuth callback only; Runtime APIs remain behind the pipe.
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ISSUER = "http://127.0.0.1:8180/realms/ai-platform"
CLIENT = "f8-desktop"
CALLBACK = "http://127.0.0.1:18765/callback"
REPO = Path(__file__).resolve().parents[3]


class Bridge:
    def __init__(self):
        self.auth = None
        self.pending = None
        self.epoch = 0
        self.online = True
        self.callback_tasks = set()
        self.summary_reads = 0
        self.diagnostic_status = None

    def clear(self):
        self.epoch += 1
        self.auth = self.pending = None

    def current(self):
        if self.auth and time.time() >= self.auth["identity"]["expiresAt"] / 1000:
            self.clear()
        return self.auth

    async def begin(self):
        if not self.online:
            raise ValueError("OFFLINE")
        self.clear()
        verifier = secrets.token_urlsafe(32)
        self.pending = {
            "state": secrets.token_urlsafe(32),
            "nonce": secrets.token_urlsafe(32),
            "verifier": verifier,
            "epoch": self.epoch,
            "until": time.monotonic() + 120,
        }
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return {
            "url": ISSUER
            + "/protocol/openid-connect/auth?"
            + urlencode(
                {
                    "client_id": CLIENT,
                    "redirect_uri": CALLBACK,
                    "response_type": "code",
                    "scope": "openid",
                    "state": self.pending["state"],
                    "nonce": self.pending["nonce"],
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "prompt": "login",
                    "max_age": "0",
                }
            )
        }

    async def callback(self, reader, writer):
        task = asyncio.current_task()
        self.callback_tasks.add(task)
        status = "400 Bad Request"
        text = "Login rejected. Return to the desktop and try again."
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            if len(raw) > 8192:
                raise ValueError("CALLBACK_TOO_LARGE")
            lines = raw.decode("ascii").split("\r\n")
            method, target, version = lines[0].split(" ")
            hosts = [x.split(":", 1)[1].strip() for x in lines[1:] if x.lower().startswith("host:")]
            parsed = urlsplit(target)
            if (
                method != "GET"
                or version != "HTTP/1.1"
                or hosts != ["127.0.0.1:18765"]
                or parsed.path != "/callback"
                or parsed.scheme
                or parsed.netloc
            ):
                raise ValueError("CALLBACK_SOURCE_REJECTED")
            query = parse_qs(parsed.query, keep_blank_values=True)
            pending = self.pending
            if not self.online or not pending or time.monotonic() > pending["until"]:
                raise ValueError("NO_LOGIN_PENDING")
            if len(query.get("state", [])) != 1 or not secrets.compare_digest(query["state"][0], pending["state"]):
                raise ValueError("STATE_REJECTED")
            if len(query.get("code", [])) != 1 or "error" in query:
                raise ValueError("CODE_REJECTED")
            self.pending = None  # Single use before any asynchronous token exchange.
            import httpx
            import jwt

            from runtime.api import auth

            async with httpx.AsyncClient(trust_env=False, timeout=5, follow_redirects=False) as client:
                response = await client.post(
                    ISSUER + "/protocol/openid-connect/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": CLIENT,
                        "redirect_uri": CALLBACK,
                        "code": query["code"][0],
                        "code_verifier": pending["verifier"],
                    },
                )
                response.raise_for_status()
                tokens = response.json()
            claims = await auth.verify_jwt(tokens["access_token"])
            principal = auth.principal_from_claims(claims)
            if principal.auth_method != "user" or principal.client_id != CLIENT:
                raise ValueError("USER_REQUIRED")
            header = jwt.get_unverified_header(tokens["id_token"])
            key = await auth._jwks_cache().get(header.get("kid", ""))
            if not key or header.get("alg") not in auth.ALLOWED_ALGORITHMS or key.algorithm_name != header["alg"]:
                raise ValueError("ID_TOKEN_KEY_REJECTED")
            identity_claims = jwt.decode(
                tokens["id_token"],
                key.key,
                algorithms=[header["alg"]],
                audience=CLIENT,
                issuer=ISSUER,
                leeway=0,
                options={"require": ["exp", "iat", "sub", "nonce"]},
            )
            if identity_claims["nonce"] != pending["nonce"] or identity_claims["sub"] != claims["sub"]:
                raise ValueError("NONCE_REJECTED")
            if pending["epoch"] != self.epoch or not self.online:
                raise ValueError("STALE_LOGIN")
            self.auth = {
                "token": tokens["access_token"],
                "identity": {
                    "issuer": ISSUER,
                    "tenant": principal.tenant_id,
                    "subject": principal.subject,
                    "username": claims.get("preferred_username", "local user"),
                    "expiresAt": min(claims["exp"], identity_claims["exp"]) * 1000,
                },
            }
            status, text = "200 OK", "Login complete. Return to the desktop window."
        except Exception:
            # Deliberately emit a fixed response, never raw OAuth errors or credentials.
            status = "400 Bad Request"
        finally:
            body = text.encode()
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    "Cache-Control: no-store\r\nContent-Security-Policy: default-src 'none'\r\n"
                    f"Referrer-Policy: no-referrer\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n"
                ).encode()
                + body
            )
            try:
                await writer.drain()
            except ConnectionError:
                pass
            writer.close()
            await writer.wait_closed()
            self.callback_tasks.discard(task)

    async def command(self, message, client):
        action = message.get("action")
        expected = {"action", "online"} if action == "set_online" else {"action"}
        if set(message) != expected:
            raise ValueError("INVALID_MESSAGE")
        if action == "login":
            return await self.begin()
        if action == "auth_status":
            current = self.current()
            return current["identity"] if current else None
        if action == "logout":
            self.clear()
            return None
        if action == "set_online":
            if type(message["online"]) is not bool:
                raise ValueError("INVALID_CONNECTION_STATE")
            self.online = message["online"]
            self.epoch += 1
            self.pending = None
            return None
        if action == "read":
            current = self.current()
            if not current:
                raise ValueError("AUTHENTICATION_REQUIRED")
            if not self.online:
                raise ValueError("OFFLINE_NO_RUNTIME_READ")
            epoch = self.epoch
            self.summary_reads += 1
            response = await client.get("/v1/desktop/summary", headers={"Authorization": "Bearer " + current["token"]})
            if response.status_code != 200:
                self.clear()
                raise ValueError("RUNTIME_AUTHORIZATION_REJECTED")
            if epoch != self.epoch or not self.current():
                raise ValueError("STALE_READ")
            return response.json()
        if action == "diagnostics":
            if self.diagnostic_status is None:
                if not self.online:
                    raise ValueError("OFFLINE_NO_RUNTIME_READ")
                unauthenticated = await client.get("/v1/workbench/identity")
                forged = await client.get("/v1/workbench/identity", headers={"X-AI-Subject": "human:forged", "X-AI-Tenant": "t2"})
                self.diagnostic_status = (unauthenticated.status_code, forged.status_code)
            # Developer-only facts, never tokens, handles, filenames or PIDs.
            return {
                "profile": "local-development-read-only",
                "real_jwks": True,
                "database": "private-temporary-sqlite",
                "worker_started": False,
                "runtime_reads": self.summary_reads,
                "unauthenticated_status": self.diagnostic_status[0],
                "forged_headers_status": self.diagnostic_status[1],
            }
        raise ValueError("INVALID_ACTION")


async def run(root):
    import httpx

    os.environ.clear()
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{root}/runtime.db",
            "WORKSPACE_DIR": str(root / "workspaces"),
            "RUNTIME_ENV": "dev",
            "RUNTIME_ROLE": "all",
            "HARNESS": "mock",
            "AUTH_REQUIRED": "true",
            "RUNTIME_ADMIN_ENABLED": "false",
            "ISOLATED_CONVERSATIONS": "1",
            "JWT_LEEWAY_S": "0",
            "JWT_ISSUER": ISSUER,
            "JWT_AUDIENCE": "ai-runtime",
            "JWKS_URL": ISSUER + "/protocol/openid-connect/certs",
            "EGRESS_ALLOWLIST": "http://127.0.0.1:8180",
            "CONSOLE_URL": "",
            "REDIS_URL": "",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
            "HOME": str(root),
            "TMPDIR": str(root),
        }
    )

    def audit(event, args):
        if event in {"subprocess.Popen", "os.system"}:
            raise RuntimeError("DESKTOP_TOOLS_DISABLED")
        if event == "socket.connect" and args[1] != ("127.0.0.1", 8180):
            raise RuntimeError("DESKTOP_EXTERNAL_NETWORK_DISABLED")
        if event == "socket.getaddrinfo" and args[0] not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("DESKTOP_EXTERNAL_DNS_DISABLED")

    sys.addaudithook(audit)
    # Verify all reviewed first-party Runtime sources before importing them.
    manifest = json.loads((root / "sources.json").read_text())
    for relative, digest in manifest.items():
        path = (REPO / relative).resolve()
        if not path.is_relative_to(REPO) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("RUNTIME_SOURCE_CHANGED")
    async with httpx.AsyncClient(trust_env=False, timeout=5) as discovery:
        configuration = (await discovery.get(ISSUER + "/.well-known/openid-configuration")).json()
        if configuration["issuer"] != ISSUER or configuration["token_endpoint"] != ISSUER + "/protocol/openid-connect/token":
            raise RuntimeError("KEYCLOAK_CONFIGURATION_REJECTED")
    from fastapi import Depends
    from sqlalchemy import func, select

    from runtime.api.auth import Principal, current_principal
    from runtime.api.workbench import user_scope
    from runtime.main import app
    from runtime.state import db

    # Deliberately do not enter Runtime.main.lifespan: no node agent, worker,
    # harness process, migration, model call or inherited production configuration.
    if db.engine.url.get_backend_name() != "sqlite" or db.engine.url.database != str(root / "runtime.db") or (root / "runtime.db").exists():
        raise RuntimeError("FRESH_DATABASE_REQUIRED")
    async with db.engine.begin() as connection:
        await connection.run_sync(db.Base.metadata.create_all)
    async with db.SessionLocal() as session:
        for index, (subject, tenant) in enumerate(
            [("human:ff800000-0000-4000-8000-000000000001", "default"), ("human:ff800000-0000-4000-8000-000000000002", "t2")]
        ):
            principal = Principal(
                subject=subject,
                tenant_id=tenant,
                client_id=CLIENT,
                actor="app:" + CLIENT,
                auth_method="user",
                token="fixture-seed",  # noqa: S106 -- scope seed only; never sent as a bearer credential
            )
            for number in range(index + 1):
                session.add(
                    db.Conversation(
                        conversation_id=f"f8-{index}-{number}",
                        scope_id=user_scope(principal),
                        tenant_id=tenant,
                        subject=subject,
                        agent_id="f8-local-fixture",
                    )
                )
        await session.commit()

    async def summary(principal=Depends(current_principal)):
        scope = user_scope(principal)
        async with db.SessionLocal() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(db.Conversation)
                .where(
                    db.Conversation.scope_id == scope,
                    db.Conversation.subject == principal.subject,
                    db.Conversation.tenant_id == principal.tenant_id,
                )
            )
        from runtime.config import settings

        return {
            "issuer": settings.jwt_issuer,
            "tenant": principal.tenant_id,
            "subject": principal.subject,
            "id": "summary",
            "synthetic": True,
            "text": f"本机隔离 Runtime：当前身份有 {count} 条合成会话。来源为临时 SQLite，未访问真实业务数据。",
        }

    app.add_api_route("/v1/desktop/summary", summary, methods=["GET"])
    bridge = Bridge()
    server = await asyncio.start_server(bridge.callback, "127.0.0.1", 18765, limit=8192)
    handshake = await asyncio.to_thread(sys.stdin.buffer.readline, 129)
    if (
        len(handshake) != 70
        or not handshake.startswith(b"F8/1 ")
        or handshake[-1:] != b"\n"
        or any(c not in b"0123456789abcdef" for c in handshake[5:69])
    ):
        raise ValueError("HANDSHAKE_REJECTED")
    sys.stdout.buffer.write(handshake)
    sys.stdout.buffer.flush()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runtime.internal") as client:
            while raw := await asyncio.to_thread(sys.stdin.buffer.readline, 8193):
                if len(raw) > 8192 or not raw.endswith(b"\n"):
                    break
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("INVALID_MESSAGE")
                    value = await bridge.command(message, client)
                    result = {"ok": True, "value": value}
                except Exception as error:
                    result = {"ok": False, "error": str(error) if isinstance(error, ValueError) else "LOCAL_RUNTIME_FAILED"}
                sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        bridge.clear()
        server.close()
        await server.wait_closed()
        tasks = list(bridge.callback_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.engine.dispose()


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) == 2 else None
    if (
        not root
        or root.parent != Path("/private/tmp")
        or not root.name.startswith("aip-f8-runtime-")
        or root.is_symlink()
        or root.stat().st_uid != os.getuid()
        or root.stat().st_mode & 0o077
    ):
        raise SystemExit("OWNED_DIRECTORY_REQUIRED")
    try:
        asyncio.run(run(root))
    except Exception as error:
        print("F8_LOCAL_START_FAILED:" + type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        shutil.rmtree(root)
