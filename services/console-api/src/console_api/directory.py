"""Tenant-scoped IdP display directory, deliberately not an authorization source."""

import unicodedata
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from console_api.auth import Operator, Role, require
from console_api.db import Audit, DirectoryTenant, DirectoryUser, SessionLocal


class MetadataRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def validated(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                raise HTTPException(422, "invalid directory input; check field lengths, identity and metadata-only contract") from error

        return validated


router = APIRouter(prefix="/directory", tags=["Console display metadata"], route_class=MetadataRoute)
RowId = Annotated[int, Path(gt=0, le=2**31 - 1)]


class DisplayMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=500)

    @field_validator("display_name", "description")
    @classmethod
    def readable(cls, value: str) -> str:
        if value != value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("metadata must have no edge whitespace or control characters")
        return value


class IdentityMetadata(DisplayMetadata):
    issuer: str = Field(min_length=1, max_length=255)
    subject: str = Field(min_length=1, max_length=128)

    @field_validator("issuer", "subject")
    @classmethod
    def stable_identity(cls, value: str) -> str:
        if any(char.isspace() or unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("identity must have no whitespace or control characters")
        return value


def user_view(row: DirectoryUser) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "issuer": row.issuer,
        "subject": row.subject,
        "display_name": row.display_name,
        "description": row.description,
    }


@router.get("/tenant")
async def get_tenant(op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = await session.get(DirectoryTenant, op.tenant)
        return {
            "tenant_id": op.tenant,
            "metadata": None if row is None else {"display_name": row.display_name, "description": row.description},
            "scope": "display_metadata_only",
        }


@router.put("/tenant")
async def put_tenant(body: DisplayMetadata, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    async with SessionLocal() as session:
        dialect = session.get_bind().dialect.name
        if dialect == "mysql":
            await session.execute(
                mysql_insert(DirectoryTenant).values(tenant_id=op.tenant, **body.model_dump()).on_duplicate_key_update(**body.model_dump())
            )
        else:
            await session.execute(
                sqlite_insert(DirectoryTenant)
                .values(tenant_id=op.tenant, **body.model_dump())
                .on_conflict_do_update(index_elements=["tenant_id"], set_=body.model_dump())
            )
        session.add(
            Audit(
                actor=op.user,
                tenant_id=op.tenant,
                action="directory.tenant.update",
                object=op.tenant,
                detail={"scope": "display_metadata_only"},
            )
        )
        await session.commit()
    return {"tenant_id": op.tenant, "metadata": body.model_dump(), "scope": "display_metadata_only"}


@router.get("/users")
async def list_users(
    after_id: int = Query(default=0, ge=0, le=2**31 - 1),
    limit: int = Query(default=100, ge=1, le=1000),
    op: Operator = Depends(require(Role.TENANT_ADMIN)),
) -> dict[str, Any]:
    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(DirectoryUser)
                .where(DirectoryUser.tenant_id == op.tenant, DirectoryUser.id > after_id)
                .order_by(DirectoryUser.id)
                .limit(limit + 1)
            )
        ).all()
        return {
            "items": [user_view(row) for row in rows[:limit]],
            "next_after_id": rows[limit - 1].id if len(rows) > limit else None,
            "scope": "display_metadata_only",
        }


@router.post("/users", status_code=201)
async def create_user(body: IdentityMetadata, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = DirectoryUser(tenant_id=op.tenant, **body.model_dump())
        session.add(row)
        try:
            await session.flush()
            session.add(
                Audit(
                    actor=op.user,
                    tenant_id=op.tenant,
                    action="directory.user.create",
                    object=str(row.id),
                    detail={"scope": "display_metadata_only"},
                )
            )
            await session.commit()
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(409, "identity mapping already exists") from error
        return user_view(row)


@router.put("/users/{row_id}")
async def update_user(row_id: RowId, body: DisplayMetadata, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> dict[str, Any]:
    async with SessionLocal() as session:
        row = (
            await session.scalars(
                select(DirectoryUser).where(DirectoryUser.id == row_id, DirectoryUser.tenant_id == op.tenant).with_for_update()
            )
        ).first()
        if row is None:
            raise HTTPException(404)
        row.display_name, row.description = body.display_name, body.description
        session.add(
            Audit(
                actor=op.user,
                tenant_id=op.tenant,
                action="directory.user.update",
                object=str(row.id),
                detail={"scope": "display_metadata_only"},
            )
        )
        await session.commit()
        return user_view(row)


@router.delete("/users/{row_id}", status_code=204)
async def delete_user(row_id: RowId, op: Operator = Depends(require(Role.TENANT_ADMIN))) -> Response:
    async with SessionLocal() as session:
        row = (
            await session.scalars(
                select(DirectoryUser).where(DirectoryUser.id == row_id, DirectoryUser.tenant_id == op.tenant).with_for_update()
            )
        ).first()
        if row is None:
            raise HTTPException(404)
        await session.delete(row)
        session.add(
            Audit(
                actor=op.user,
                tenant_id=op.tenant,
                action="directory.user.delete",
                object=str(row_id),
                detail={"scope": "display_metadata_only", "authorization_changed": False},
            )
        )
        await session.commit()
    return Response(status_code=204)
