import json
import logging
import re
import asyncio

from app.config import settings
from app.services import dhcp_service

logger = logging.getLogger(__name__)

# Matches -SharedSecret "..." (including empty string) for log redaction.
_SECRET_RE = re.compile(r'(-SharedSecret\s+)"[^"]*"', re.IGNORECASE)
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s,;]+")


def redact_powershell_command(command: str) -> str:
    """Remove sensitive parameter values from a command string before logging."""
    return _SECRET_RE.sub(r'\1"***REDACTED***"', command)


def _sanitize_stderr_for_log(stderr: str) -> str:
    return _WIN_PATH_RE.sub("<path>", stderr)


class PowerShellError(Exception):
    def __init__(self, command: str, stderr: str, returncode: int):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"PowerShell command failed (rc={returncode}): {stderr}")


class PowerShellExecutionError(PowerShellError):
    """PowerShell exited non-zero or produced unusable output."""


class PowerShellTimeoutError(PowerShellError):
    """PowerShell process exceeded the configured timeout."""

    def __init__(self, command: str, timeout_seconds: int):
        self.timeout_seconds = timeout_seconds
        super().__init__(
            command,
            f"PowerShell command timed out after {timeout_seconds} seconds",
            -1,
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


async def run_ps(
    command: str,
    parse_json: bool = True,
    *,
    append_error_action: bool = True,
    append_convert_to_json: bool = True,
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

    logger.info("PS> %s", redact_powershell_command(command))

    process: asyncio.subprocess.Process | None = None
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
        ) from exc

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")

    if process.returncode != 0:
        logger.error(
            "PS FAILED (rc=%d): %s",
            process.returncode,
            _sanitize_stderr_for_log(stderr.strip()),
        )
        raise PowerShellExecutionError(command, stderr.strip(), process.returncode or 1)

    logger.debug("PS OUT: %s", stdout.strip()[:500])

    if not parse_json or not stdout.strip():
        return None

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PowerShellExecutionError(
            command,
            f"PowerShell returned non-JSON output: {exc}. stdout={stdout.strip()[:200]!r}",
            0,
        ) from exc
