from __future__ import annotations
import functools
import logging
import time
from typing import Callable, TypeVar

from fastapi import HTTPException, status

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
    - DhcpApiError subclass  → HTTPException(exc.http_status, exc.detail)
    - PowerShellError (not found) → HTTPException(404, "Scope {scope_id} not found")
    - PowerShellError (other)     → re-raise (global handler → HTTP 500)

    scope_id for 404 messages is extracted from the first positional argument,
    matching the convention used by all public service functions.
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
                scope_id = args[0] if args else "unknown"
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Scope {scope_id} not found",
                ) from exc
            raise

    return wrapper  # type: ignore[return-value]
