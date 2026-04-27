from __future__ import annotations
import functools
import logging
import time
from typing import Callable, ParamSpec, TypeVar


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
