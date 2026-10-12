"""Strict public organization routes; identities come from the authenticated boundary."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, field_validator
import re

from production.api.security import require_permission
from production.api.tasks import idempotency_key
from production.auth.routes import protocol_query
from production.contracts import Actor, ContractModel, ContractViolation, OrganizationRole
from production.organizations import OrganizationService


class OrganizationCreate(ContractModel):
    name: str = Field(min_length=1, max_length=100)


class InvitationCreate(ContractModel):
    email: str = Field(min_length=1, max_length=320)
    role: OrganizationRole

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^@\s<>]+@(?:[^@\s<>.]+\.)+[^@\s<>.]+", value):
            raise ValueError("Invalid email")
        return value.casefold()


class InvitationAccept(ContractModel):
    ticket: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$", repr=False)


class MemberRoleChange(ContractModel):
    role: OrganizationRole


def pagination(request):
    query = protocol_query(request, {"limit", "after"})
    try:
        limit = int(query.get("limit", "20"))
        if not 1 <= limit <= 100:
            raise ValueError
    except ValueError:
        raise ContractViolation("Invalid pagination") from None
    return {"limit": limit, "after": query.get("after")}


def operation_response(result):
    if result.body is None:
        return Response(status_code=result.status_code)
    body = result.body.model_dump(mode="json")
    return JSONResponse({"error": body} if result.status_code >= 400 else body, status_code=result.status_code)


def mount_organizations(app, runtime):
    service = OrganizationService(runtime)
    app.state.organization_service = service
    router = APIRouter()

    async def policy(request, context):
        path = request.url.path.rstrip("/")
        if path == "/api/invitations/accept":
            return
        if path == "/api/organizations":
            require_permission(context, "organization:create" if request.method == "POST" else "organization:read")
            return
        parts = path.split("/")
        if len(parts) == 4 and parts[:3] == ["", "api", "organizations"]:
            if context.workspace.kind != "organization" or context.workspace.owner_id != parts[3]:
                raise ContractViolation("Not found", "not_found")
            require_permission(context, "organization:read")
            return
        if len(parts) == 5 and parts[:3] == ["", "api", "organizations"] and parts[4] in {"members", "invitations"}:
            if context.workspace.kind != "organization" or context.workspace.owner_id != parts[3]:
                raise ContractViolation("Not found", "not_found")
            require_permission(context, "organization:manage")
            return
        if len(parts) == 6 and parts[:3] == ["", "api", "organizations"] and parts[4] == "members":
            if context.workspace.kind != "organization" or context.workspace.owner_id != parts[3]:
                raise ContractViolation("Not found", "not_found")
            await service.authorize_member(context, parts[3], parts[5], "member:role" if request.method == "PATCH" else "member:remove",
                request.headers.get("idempotency-key"))
            require_permission(context, "organization:manage")
            return
        if len(parts) == 6 and parts[:3] == ["", "api", "organizations"] and parts[4] == "invitations":
            if context.workspace.kind != "organization" or context.workspace.owner_id != parts[3]:
                raise ContractViolation("Not found", "not_found")
            await service.stored("get_invitation", Actor(identity=context.identity, workspace=context.workspace), parts[5])
            require_permission(context, "organization:manage")
            return
        raise ContractViolation("Not found", "not_found")

    @router.get("/api/organizations")
    async def organizations(request: Request):
        return await service.list_organizations(request.state.auth_context, **pagination(request))

    @router.post("/api/organizations")
    async def create(body: OrganizationCreate, request: Request):
        return operation_response(await service.create_organization(request.state.auth_context, body.name,
            idempotency_key(request.headers.get("idempotency-key"))))

    @router.get("/api/organizations/{organization_id}")
    async def detail(organization_id: str, request: Request):
        return await service.organization_detail(request.state.auth_context, request.state.auth_workspace_name)

    @router.get("/api/organizations/{organization_id}/members")
    async def members(organization_id: str, request: Request):
        return await service.list_members(request.state.auth_context, **pagination(request))

    @router.post("/api/organizations/{organization_id}/invitations")
    async def invite(organization_id: str, body: InvitationCreate, request: Request):
        return operation_response(await service.create_invitation(request.state.auth_context, body.email, body.role,
            idempotency_key(request.headers.get("idempotency-key"))))

    @router.get("/api/organizations/{organization_id}/invitations")
    async def invitations(organization_id: str, request: Request):
        return await service.list_invitations(request.state.auth_context, **pagination(request))

    @router.delete("/api/organizations/{organization_id}/invitations/{invitation_id}")
    async def cancel_invitation(organization_id: str, invitation_id: str, request: Request):
        return operation_response(await service.cancel_invitation(request.state.auth_context, invitation_id,
            idempotency_key(request.headers.get("idempotency-key"))))

    @router.post("/api/invitations/accept")
    async def accept(body: InvitationAccept, request: Request):
        return operation_response(await service.accept_invitation(request.state.auth_context, request.state.auth_user, body.ticket,
            idempotency_key(request.headers.get("idempotency-key"))))

    @router.patch("/api/organizations/{organization_id}/members/{user_id}")
    async def change_role(organization_id: str, user_id: str, body: MemberRoleChange, request: Request):
        return operation_response(await service.change_member(request.state.auth_context, user_id, body.role,
            idempotency_key(request.headers.get("idempotency-key"))))

    @router.delete("/api/organizations/{organization_id}/members/{user_id}")
    async def remove_member(organization_id: str, user_id: str, request: Request):
        return operation_response(await service.change_member(request.state.auth_context, user_id, None,
            idempotency_key(request.headers.get("idempotency-key"))))

    app.include_router(router)
    app.state.auth_policies["organizations"] = policy
