from __future__ import annotations


class DhcpApiError(Exception):
    """Base domain exception. Subclasses carry their own HTTP status code.

    Raised in route dependency functions (auth, input validation) in place of
    HTTPException.  Converted to HTTP responses by two mechanisms:

    1. handle_http_errors decorator — catches DhcpApiError from within service
       calls and re-raises as HTTPException.
    2. Global exception handler (exception_handlers.py) — catches DhcpApiError
       raised from FastAPI dependency functions, which run before the route
       handler and are therefore outside the service-layer decorator stack.

    Note: PowerShell "not found" errors produce 404 responses via the
    PowerShellError branch of handle_http_errors, not via NotFoundError.
    """
    http_status: int = 500

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class BadRequestError(DhcpApiError):
    http_status = 400


class UnauthorizedError(DhcpApiError):
    http_status = 401
