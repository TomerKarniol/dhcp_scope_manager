from __future__ import annotations

from ipaddress import AddressValueError, IPv4Address
from typing import Annotated

from fastapi import Body, Depends

from app.errors import InvalidScopeIdError, ScopeIdMismatchError
from app.models import DhcpScopePayload
from app.utils.decorators import log_call


@log_call
async def validate_scope_id(scope_id: str) -> str:
    try:
        return str(IPv4Address(scope_id))
    except (AddressValueError, ValueError):
        raise InvalidScopeIdError(scope_id)


@log_call
async def validate_scope_request(
    payload: Annotated[DhcpScopePayload, Body(...)],
    scope_id: str = Depends(validate_scope_id),
) -> tuple[str, DhcpScopePayload]:
    payload_network = str(payload.network)

    if payload_network != scope_id:
        raise ScopeIdMismatchError(scope_id, payload_network)

    return scope_id, payload
