import json
import logging
import re
import asyncio
import time

from app.config import settings
from app.services import dhcp_service

logger = logging.getLogger(__name__)

_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s,;]+")
_MAX_STDERR_PREVIEW_LEN = 500


def sanitize_powershell_text(value: str, *, max_len: int = _MAX_STDERR_PREVIEW_LEN) -> str:
    """Remove high-risk infrastructure details from log/client text."""
    redacted = _WIN_PATH_RE.sub("<path>", value)
    return redacted[:max_len]


def _sanitize_stderr_for_log(stderr: str) -> str:
    return sanitize_powershell_text(stderr)


class PowerShellError(Exception):
    def __init__(
        self,
        command: str,
        stderr: str,
        returncode: int,
        *,
        operation: str | None = None,
        scope_id: str | None = None,
    ):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        self.operation = operation
        self.scope_id = scope_id
        super().__init__(self.safe_message)

    @property
    def safe_stderr_preview(self) -> str:
        return sanitize_powershell_text(self.stderr)

    @property
    def safe_message(self) -> str:
        operation = self.operation or "unknown"
        return (
            f"PowerShell command failed "
            f"(operation={operation}, rc={self.returncode}): {self.safe_stderr_preview}"
        )

    def __str__(self) -> str:
        return self.safe_message


class PowerShellExecutionError(PowerShellError):
    """PowerShell exited non-zero or produced unusable output."""


class PowerShellTimeoutError(PowerShellError):
    """PowerShell process exceeded the configured timeout."""

    def __init__(
        self,
        command: str,
        timeout_seconds: int,
        *,
        operation: str | None = None,
        scope_id: str | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        super().__init__(
            command,
            f"PowerShell command timed out after {timeout_seconds} seconds",
            -1,
            operation=operation,
            scope_id=scope_id,
        )


_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None
_semaphore_limit: int | None = None


def _get_powershell_semaphore() -> asyncio.Semaphore:
    """Return a semaphore bound to the current event loop.

    pytest and ASGI servers may use different event loops over the process
    lifetime, so the semaphore is created lazily for the active loop.
    """
    global _semaphore, _semaphore_loop, _semaphore_limit

    loop = asyncio.get_running_loop()
    limit = settings.POWERSHELL_MAX_CONCURRENCY
    if _semaphore is None or _semaphore_loop is not loop or _semaphore_limit != limit:
        _semaphore = asyncio.Semaphore(limit)
        _semaphore_loop = loop
        _semaphore_limit = limit
    return _semaphore


def is_not_found_error(stderr: str) -> bool:
    """Return True if PowerShell stderr indicates the requested object does not exist."""
    lower = stderr.lower()
    return any(kw in lower for kw in ("not found", "does not exist", "no dhcp scope", "cannot find"))


def is_already_exists_error(stderr: str) -> bool:
    """Return True if PowerShell stderr indicates the object already exists."""
    lower = stderr.lower()
    return any(kw in lower for kw in ("already exists", "already been added", "already in use"))


async def run_ps(
    command: str,
    parse_json: bool = True,
    *,
    append_error_action: bool = True,
    append_convert_to_json: bool = True,
    scope_id: str | None = None,
    operation: str | None = None,
    relationship_name: str | None = None,
) -> dict | list | None:
    """Execute a PowerShell command and optionally parse JSON output.

    By default, appends -ErrorAction Stop so errors raise PowerShellError
    instead of silently returning empty output, and appends ConvertTo-Json for
    callers that want parsed JSON from a plain PowerShell object.

    Set append_error_action=False and append_convert_to_json=False only for
    complete scripts that already handle per-cmdlet errors and emit JSON.

    Execution-layer guard: validates DHCP environment before every call.
    This is a mandatory safety net — even if route-level protection is bypassed,
    DHCP operations will not proceed in unsupported environments.
    The validation result is cached so this check is free after the first call.

    Raises:
        DhcpEnvironmentError: if the runtime cannot support DHCP automation.
        PowerShellError: if the PowerShell command exits with a non-zero code.
    """
    await dhcp_service.validate_dhcp_environment()

    full_cmd = command
    if append_error_action:
        full_cmd = f"{full_cmd} -ErrorAction Stop"
    if parse_json and append_convert_to_json:
        full_cmd += " | ConvertTo-Json -Depth 5 -Compress"

    log_extra = {
        "scope_id": scope_id,
        "operation": operation or "powershell",
        "relationship_name": relationship_name,
    }
    logger.info("Running DHCP PowerShell command", extra=log_extra)

    process: asyncio.subprocess.Process | None = None
    t0 = time.monotonic()
    try:
        async with _get_powershell_semaphore():
            process = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=settings.POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError as exc:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        raise PowerShellTimeoutError(
            command,
            settings.POWERSHELL_COMMAND_TIMEOUT_SECONDS,
            operation=operation,
            scope_id=scope_id,
        ) from exc

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    duration_ms = round((time.monotonic() - t0) * 1000, 2)

    if process.returncode != 0:
        logger.error(
            "DHCP PowerShell command failed",
            extra={
                **log_extra,
                "duration_ms": duration_ms,
                "status": "failed",
                "returncode": process.returncode,
                "stderr_preview": _sanitize_stderr_for_log(stderr.strip()),
            },
        )
        raise PowerShellExecutionError(
            command,
            stderr.strip(),
            process.returncode or 1,
            operation=operation,
            scope_id=scope_id,
        )

    logger.info(
        "DHCP PowerShell command completed",
        extra={**log_extra, "duration_ms": duration_ms, "status": "ok"},
    )

    if not parse_json or not stdout.strip():
        return None

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PowerShellExecutionError(
            command,
            f"PowerShell returned non-JSON output: {exc}. stdout={stdout.strip()[:200]!r}",
            0,
            operation=operation,
            scope_id=scope_id,
        ) from exc
