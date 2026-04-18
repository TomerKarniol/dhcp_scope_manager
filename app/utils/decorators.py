from __future__ import annotations
import functools
import logging
import time
from typing import Any, Callable, ParamSpec, TypeVar

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

from app.services.dhcp_service import DhcpEnvironmentError
from app.services.ps_executor import PowerShellError, is_not_found_error

P = ParamSpec("P")
R = TypeVar("R")


def log_call(func: Callable[P, R]) -> Callable[P, R]:
    """Log entry, exit, and wall-clock duration of any service function."""
    logger = logging.getLogger(func.__module__)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        logger.info("→ %s", func.__name__)
        t0 = time.monotonic()
        try:
            result = func(*args, **kwargs)
            logger.info("← %s (%.3fs)", func.__name__, time.monotonic() - t0)
            return result
        except Exception:
            logger.info("← %s raised (%.3fs)", func.__name__, time.monotonic() - t0)
            raise

    return wrapper


def handle_http_errors(func: Callable[P, R]) -> Callable[P, R]:
    """Translate PowerShell not-found errors into HTTPException(404) at the HTTP boundary.

    Handles:
    - PowerShellError (not found) → HTTPException(404, "Scope {scope_id} not found")
    - PowerShellError (other)     → re-raise (global handler → HTTP 500)
    - DhcpEnvironmentError        → re-raise (global handler → HTTP 503)

    scope_id for 404 messages is resolved from kwargs["scope_id"] first, then
    the first positional argument — matching the convention of all service functions
    where the first parameter is always the scope network address.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except PowerShellError as exc:
            if is_not_found_error(exc.stderr):
                scope_id = kwargs.get("scope_id") or (str(args[0]) if args else "unknown")
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Scope {scope_id} not found",
                ) from exc
            raise

    return wrapper


def handle_health_errors(func: Callable[P, Any]) -> Callable[P, Any]:
    """Route-level decorator for /healthz: converts DhcpEnvironmentError into a 503
    JSONResponse with {status, detail, reason} shape.

    Applied to the route handler (not the service function) so HTTP translation
    stays in the API layer. Other exceptions propagate as-is so genuine bugs
    surface as 500, not 503.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        try:
            return func(*args, **kwargs)
        except DhcpEnvironmentError as exc:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "error", "detail": exc.detail, "reason": exc.reason},
            )

    return wrapper
