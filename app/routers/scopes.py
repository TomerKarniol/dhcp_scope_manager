from __future__ import annotations
import hmac
from ipaddress import IPv4Address, AddressValueError

from fastapi import APIRouter, Depends, Header, Response, status

from app.config import settings
from app.models import DhcpScopePayload
from app.services import dhcp_env, scope_service
from app.utils.exceptions import BadRequestError, UnauthorizedError


def _require_dhcp_env() -> None:
    dhcp_env.validate_dhcp_environment()


router = APIRouter(
    prefix="/api/v1",
    tags=["scopes"],
    dependencies=[Depends(_require_dhcp_env)],
)


def _verify_token(authorization: str = Header(default="")) -> None:
    if not settings.DHCP_API_TOKEN:
        return
    expected = f"Bearer {settings.DHCP_API_TOKEN}"
    if not hmac.compare_digest(authorization, expected):
        raise UnauthorizedError("Unauthorized")


def _validate_scope_id(scope_id: str) -> str:
    try:
        IPv4Address(scope_id)
    except (AddressValueError, ValueError):
        raise BadRequestError(f"Invalid scope ID '{scope_id}': each octet must be 0–255")
    return scope_id


@router.get("/scopes", response_model=list[DhcpScopePayload], status_code=status.HTTP_200_OK)
def list_scopes(_: None = Depends(_verify_token)) -> list[DhcpScopePayload]:
    return scope_service.list_scopes()


@router.post("/scopes/{scope_id}", response_model=DhcpScopePayload, status_code=status.HTTP_200_OK)
def create_scope_by_id(
    payload: DhcpScopePayload,
    scope_id: str = Depends(_validate_scope_id),
    _: None = Depends(_verify_token),
) -> DhcpScopePayload:
    if str(payload.network) != scope_id:
        raise BadRequestError(
            f"scope_id '{scope_id}' does not match network '{payload.network}' in body"
        )
    return scope_service.create_scope(payload)


@router.get("/scopes/{scope_id}", response_model=DhcpScopePayload, status_code=status.HTTP_200_OK)
def get_scope(
    scope_id: str = Depends(_validate_scope_id),
    _: None = Depends(_verify_token),
) -> DhcpScopePayload:
    return scope_service.get_scope(scope_id)


@router.put("/scopes/{scope_id}", response_model=DhcpScopePayload, status_code=status.HTTP_200_OK)
def update_scope(
    payload: DhcpScopePayload,
    scope_id: str = Depends(_validate_scope_id),
    _: None = Depends(_verify_token),
) -> DhcpScopePayload:
    if str(payload.network) != scope_id:
        raise BadRequestError(
            f"scope_id '{scope_id}' does not match network '{payload.network}' in body"
        )
    return scope_service.update_scope(scope_id, payload)


@router.delete("/scopes/{scope_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_scope(
    scope_id: str = Depends(_validate_scope_id),
    _: None = Depends(_verify_token),
) -> Response:
    scope_service.delete_scope(scope_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
