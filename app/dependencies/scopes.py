from __future__ import annotations

from ipaddress import AddressValueError, IPv4Address
from typing import Annotated

from fastapi import Body, Depends

from app.models import DhcpScopePayload
from app.utils.exceptions import BadRequestError


def validate_scope_id(scope_id: str) -> str:
    try:
        IPv4Address(scope_id)
    except (AddressValueError, ValueError):
        raise BadRequestError(f"Invalid scope ID '{scope_id}': each octet must be 0–255")
    return scope_id


def validate_scope_request(
    payload: Annotated[DhcpScopePayload, Body(...)],
    scope_id: str = Depends(validate_scope_id),
) -> tuple[str, DhcpScopePayload]:
    payload_network = str(payload.network)

    if payload_network != scope_id:
        raise BadRequestError(
            f"scope_id '{scope_id}' does not match network '{payload_network}' in body"
        )

    return scope_id, payload
