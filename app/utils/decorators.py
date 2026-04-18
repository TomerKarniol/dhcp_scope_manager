from __future__ import annotations
import functools
import logging
import time
from typing import Callable, TypeVar

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

# Circular import note:
#   dhcp_service → decorators → ps_executor → dhcp_service (module-level reference)
# ps_executor imports the dhcp_service MODULE (not a specific name), so Python's
# partial-module mechanism resolves the cycle at import time.  DhcpEnvironmentError
# is imported lazily inside handle_health_errors (at call time) for the same reason.
from app.services.ps_executor import PowerShellError, is_not_found_error
from app.utils.exceptions import DhcpApiError

F = TypeVar("F", bound=Callable)


def log_call(func: F) -> F:
    """Log entry, exit, and wall-clock duration of any service function."""
    logger = logging.getLogger(func.__module__)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        logger.info("→ %s", func.__name__)
        t0 = time.monotonic()
        try:
            result = func(*args, **kwargs)
            logger.info("← %s (%.3fs)", func.__name__, time.monotonic() - t0)
            return result
        except Exception:
            logger.info("← %s raised (%.3fs)", func.__name__, time.monotonic() - t0)
            raise

    return wrapper  # type: ignore[return-value]


def handle_http_errors(func: F) -> F:
    """Translate domain and PowerShell errors into HTTPException at the HTTP boundary.

    Handles (in order):
    - DhcpApiError subclass       → HTTPException(exc.http_status, exc.detail)
    - PowerShellError (not found) → HTTPException(404, "Scope {scope_id} not found")
    - PowerShellError (other)     → re-raise (global handler → HTTP 500)

    The DhcpApiError branch is forward insurance: current decorated functions
    (get_scope, update_scope) do not raise DhcpApiError, but if service-layer
    validation is ever added, it will be translated correctly without changes here.

    scope_id for 404 messages is resolved from kwargs["scope_id"] first, then
    the first positional argument — matching the convention of all service functions
    where the first parameter is always the scope network address.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except DhcpApiError as exc:
            raise HTTPException(
                status_code=exc.http_status,
                detail=exc.detail,
            ) from exc
        except PowerShellError as exc:
            if is_not_found_error(exc.stderr):
                scope_id = kwargs.get("scope_id") or (str(args[0]) if args else "unknown")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Scope {scope_id} not found",
                ) from exc
            raise

    return wrapper  # type: ignore[return-value]


def handle_health_errors(func: F) -> F:
    """For the /healthz route: converts DhcpEnvironmentError and any unexpected
    Exception into a 503 JSONResponse with {status, detail, reason} shape.

    Keeps the health endpoint always callable even in broken environments —
    it must never raise, only report.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from app.services.dhcp_service import DhcpEnvironmentError  # lazy — breaks circular import
        try:
            return func(*args, **kwargs)
        except DhcpEnvironmentError as exc:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "error", "detail": exc.detail, "reason": exc.reason},
            )
        except Exception as exc:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "error", "detail": str(exc)},
            )

    return wrapper  # type: ignore[return-value]
