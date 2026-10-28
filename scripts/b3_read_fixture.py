"""B3 signed read bridge, installed only by the private local demo fixture."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec


def install(provider, synthetic):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/capability-gateway/tests"))
    from gateway_testkit import Upstream

    from capability_gateway import act, auth, config, main, state
    from capability_gateway.catalog import ConnectorVersion, MemoryCatalog
    from mcp_template import jwks
    from mcp_template.act import ActConfig, verify_act_assertion
    from runtime.config import settings
    from runtime.isolated_read import invoke_read
    from runtime.loop import toolbroker
    from runtime.state import db

    upstream = Upstream()
    state.catalog = MemoryCatalog()
    config.KEYCLOAK_TOKEN_URL = settings.jwt_issuer + "/protocol/openid-connect/token"
    config.JWT_ISSUER = settings.jwt_issuer
    config.JWKS_URL = settings.jwks_url
    config.SERVICE_AUDIENCE = "capability-gateway"
    config.USER_AUDIENCE = frozenset({"ai-runtime"})
    config.TRUSTED_CALLERS = frozenset({"ai-runtime-svc"})
    config.POLICY_URL = "http://policy.test"
    act.reset_for_tests(ec.generate_private_key(ec.SECP256R1()))
    auth.reset_for_tests()
    jwks.set_transport_for_tests(httpx.MockTransport(lambda request: httpx.Response(200, json=act.jwks())))
    keys = provider._jwks(httpx.Request("GET", settings.jwks_url)).json()["keys"]

    def verify(token, audience):
        header = jwt.get_unverified_header(token)
        key = next(k for k in keys if k["kid"] == header["kid"])
        return jwt.decode(token, jwt.PyJWK(key).key, algorithms=["RS256"], audience=audience, issuer=settings.jwt_issuer)

    async def transport(request):
        if request.url.host == "idp.test":
            if request.url.path.endswith("/certs"):
                return provider._jwks(request)
            form = parse_qs(request.content.decode())
            claims = verify(form["subject_token"][0], "ai-runtime")
            token = provider.token({"sub": claims["sub"], "aud": "mcp-ticket"})
            return httpx.Response(200, json={"access_token": token, "expires_in": 60})
        if request.url.host == "ticket.test" and request.url.path == "/tools/ticket.get":
            obo = request.headers["authorization"].removeprefix("Bearer ")
            claims = verify(obo, "mcp-ticket")
            context = await verify_act_assertion(
                request.headers[act.HEADER],
                obo_token=obo,
                obo_sub=claims["sub"],
                cfg=ActConfig(jwks_url="http://test.invalid/jwks", issuer=config.ACT_ISSUER, audience="mcp-ticket"),
            )
            assert context.verified and context.chain[-1] == "client:ai-runtime-svc"
            from ai_contract import RiskLevel, ToolSpec

            output = await synthetic(
                ToolSpec(name="ticket.get", description="Synthetic read", risk_level=RiskLevel.READ), json.loads(request.content)
            )
            print("B3_VERIFIED " + json.dumps({"run_id": context.run_id, "call_id": context.call_id, "verified": True}), flush=True)
            return httpx.Response(503 if "error" in output else 200, json=output)
        if request.url.host in {"policy.test", "ticket.test"}:
            return upstream(request)
        raise AssertionError("unexpected isolated upstream")

    main.set_invoke_transport_for_tests(httpx.MockTransport(transport))

    async def execute(tool, tool_input, dry_run=False, rc=None, call_id=None, idempotency_key=None):
        if tool.name != "ticket.get" or dry_run or rc is None or not rc.credential or not call_id:
            raise toolbroker.GatewayToolError("B3 demo accepts only an authenticated pending read")
        await state.catalog.put_connector(
            ConnectorVersion(name="mcp-ticket", endpoint="http://ticket.test", system="ticket", status="active")
        )
        service = provider.token({"sub": "sa-runtime", "azp": "ai-runtime-svc", "aud": "capability-gateway"})
        try:
            async with db.SessionLocal() as session:
                result = await invoke_read(
                    session,
                    run_id=rc.run_id,
                    call_id=call_id,
                    user_token=rc.credential,
                    service_token=service,
                    gateway=httpx.ASGITransport(app=main.app),
                )
        except Exception as exc:
            print("B3_REJECTED " + type(exc).__name__ + ": " + str(exc), flush=True)
            raise toolbroker.GatewayToolError("isolated read rejected") from exc
        return result["output"]

    toolbroker.execute_tool = execute
