from __future__ import annotations
import functools
import inspect
import logging
import time
from typing import Callable, ParamSpec, TypeVar, cast


P = ParamSpec("P")
R = TypeVar("R")


def log_call(func: Callable[P, R]) -> Callable[P, R]:
    """Log entry, exit, duration, scope_id, and status for any service function.

    Structured fields emitted via ``extra`` (picked up by _SafeJsonFormatter):
      - ``operation``   — always set to the function name
      - ``scope_id``    — set when the function has a ``scope_id`` parameter
      - ``duration_ms`` — set on exit (both success and error)
      - ``status``      — ``"ok"`` on success, ``"error"`` on exception

    scope_id is extracted by inspecting the bound arguments at call time so no
    call-site changes are needed.  If binding fails for any reason scope_id is
    omitted rather than crashing.
    """
    logger = logging.getLogger(func.__module__)
    signature = inspect.signature(func, eval_str=True)

    def _entry_extra(scope_id: object) -> dict[str, object]:
        extra: dict[str, object] = {"operation": func.__name__}
        if scope_id is not None:
            extra["scope_id"] = str(scope_id)
        return extra

    def _exit_extra(scope_id: object, status: str, duration_ms: float) -> dict[str, object]:
        extra: dict[str, object] = {
            "operation": func.__name__,
            "status": status,
            "duration_ms": duration_ms,
        }
        if scope_id is not None:
            extra["scope_id"] = str(scope_id)
        return extra

    def _extract_scope_id(*args: object, **kwargs: object) -> object:
        try:
            bound = signature.bind_partial(*args, **kwargs)
            return bound.arguments.get("scope_id")
        except TypeError:
            return None

    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs):
            scope_id = _extract_scope_id(*args, **kwargs)
            logger.info("→ %s", func.__name__, extra=_entry_extra(scope_id))
            t0 = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                dur = round((time.monotonic() - t0) * 1000, 2)
                logger.info("← %s", func.__name__, extra=_exit_extra(scope_id, "ok", dur))
                return result
            except Exception:
                dur = round((time.monotonic() - t0) * 1000, 2)
                logger.info("← %s raised", func.__name__, extra=_exit_extra(scope_id, "error", dur))
                raise

        async_wrapper.__signature__ = signature  # type: ignore[attr-defined]
        return cast(Callable[P, R], async_wrapper)

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        scope_id = _extract_scope_id(*args, **kwargs)
        logger.info("→ %s", func.__name__, extra=_entry_extra(scope_id))
        t0 = time.monotonic()
        try:
            result = func(*args, **kwargs)
            dur = round((time.monotonic() - t0) * 1000, 2)
            logger.info("← %s", func.__name__, extra=_exit_extra(scope_id, "ok", dur))
            return result
        except Exception:
            dur = round((time.monotonic() - t0) * 1000, 2)
            logger.info("← %s raised", func.__name__, extra=_exit_extra(scope_id, "error", dur))
            raise

    wrapper.__signature__ = signature  # type: ignore[attr-defined]
    return cast(Callable[P, R], wrapper)
