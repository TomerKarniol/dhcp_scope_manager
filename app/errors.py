from __future__ import annotations

from typing import Any

from fastapi import status


class ErrorCode:
    INVALID_SCOPE_ID = "INVALID_SCOPE_ID"
    SCOPE_ID_MISMATCH = "SCOPE_ID_MISMATCH"
    UNAUTHORIZED = "UNAUTHORIZED"
    SCOPE_NOT_FOUND = "SCOPE_NOT_FOUND"
    DHCP_CONFLICT = "DHCP_CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    POWERSHELL_COMMAND_FAILED = "POWERSHELL_COMMAND_FAILED"
    POWERSHELL_TIMEOUT = "POWERSHELL_TIMEOUT"
    DHCP_ENVIRONMENT_UNAVAILABLE = "DHCP_ENVIRONMENT_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    HTTP_ERROR = "HTTP_ERROR"


class AppError(Exception):
    """Base class for expected, client-safe application errors."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = ErrorCode.INTERNAL_ERROR
    message = "Internal server error"
    details: dict[str, Any] = {}
    headers: dict[str, str] | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if message is not None:
            self.message = message
        self.details = details or {}
        self.headers = headers
        super().__init__(self.message)


class InvalidScopeIdError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = ErrorCode.INVALID_SCOPE_ID

    def __init__(self, scope_id: str) -> None:
        super().__init__(
            f"Scope ID '{scope_id}' is not a valid IPv4 address",
            details={"scopeId": scope_id},
        )


class ScopeIdMismatchError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = ErrorCode.SCOPE_ID_MISMATCH

    def __init__(self, scope_id: str, payload_network: str) -> None:
        super().__init__(
            f"scope_id '{scope_id}' does not match payload network '{payload_network}'",
            details={"scopeId": scope_id, "network": payload_network},
        )


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = ErrorCode.UNAUTHORIZED

    def __init__(self) -> None:
        super().__init__(
            "Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


class ScopeNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = ErrorCode.SCOPE_NOT_FOUND

    def __init__(self, scope_id: str) -> None:
        self.scope_id = scope_id
        super().__init__(
            f"DHCP scope {scope_id} was not found",
            details={"scopeId": scope_id},
        )


class DhcpConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = ErrorCode.DHCP_CONFLICT

    def __init__(self, message: str = "DHCP state conflicts with the request") -> None:
        super().__init__(message)
