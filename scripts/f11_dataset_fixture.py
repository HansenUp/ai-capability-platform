"""Only installed by the disposable browser fixture, never a production identity adapter."""
from fastapi import HTTPException
from sqlalchemy import select


def install(app, issuer):
    from console_api.auth import Operator, Role, assert_assigned
    from console_api.db import Agent, SessionLocal
    from runtime.api import isolated_datasets as datasets

    # No browser header or user-supplied role participates in this mapping.
    app.state.isolated_dataset_bindings = {
        (issuer, "default", "human:dataset_admin"): "dataset-admin",
        (issuer, "default", "human:dataset_unassigned"): "dataset-unassigned",
    }
    roles = {("default", "dataset-admin"): Role.ADMIN, ("default", "dataset-unassigned"): Role.ADMIN}
    app.state.isolated_dataset_test_roles = roles

    async def authorize(tenant, user, agent_id):
        role = roles.get((tenant, user), Role.USER)
        if role < Role.ADMIN:
            raise HTTPException(403, "dataset management role required")
        async with SessionLocal() as session:
            if not await session.scalar(select(Agent.id).where(Agent.tenant_id == tenant, Agent.agent_id == agent_id)):
                raise HTTPException(404, "agent not found")
            await assert_assigned(session, Operator(user, tenant, role), agent_id)

    app.state.isolated_dataset_authorize = authorize
    app.state.isolated_datasets = datasets.ENABLED


def seed(console):
    import httpx

    with httpx.Client(base_url=console, trust_env=False, timeout=10) as client:
        assert client.post("/agents", json={"agent_id": "f11-dataset-demo", "version": "1"}).status_code == 201
        assert client.put("/agents/f11-dataset-demo/admins", json={"users": ["dataset-admin"]}).status_code == 200
