from __future__ import annotations


class DhcpApiError(Exception):
    """Base domain exception. Subclasses carry their own HTTP status code.

    Raised anywhere in route or service code in place of HTTPException.
    Converted to JSONResponse by handle_http_errors decorator and the global
    exception handler (for errors raised inside FastAPI dependency functions).
    """
    http_status: int = 500

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class BadRequestError(DhcpApiError):
    http_status = 400


class UnauthorizedError(DhcpApiError):
    http_status = 401


class NotFoundError(DhcpApiError):
    http_status = 404


class ConflictError(DhcpApiError):
    http_status = 409
