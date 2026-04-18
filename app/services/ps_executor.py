import json
import logging
import re
import subprocess

from app.services import dhcp_service

logger = logging.getLogger(__name__)

# Matches -SharedSecret "..." (including empty string) for log redaction.
_SECRET_RE = re.compile(r'(-SharedSecret\s+)"[^"]*"', re.IGNORECASE)


def _redact_secrets(command: str) -> str:
    """Remove sensitive parameter values from a command string before logging."""
    return _SECRET_RE.sub(r'\1"***REDACTED***"', command)


class PowerShellError(Exception):
    def __init__(self, command: str, stderr: str, returncode: int):
        self.command = command
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(f"PowerShell command failed (rc={returncode}): {stderr}")


def is_not_found_error(stderr: str) -> bool:
    """Return True if PowerShell stderr indicates the requested object does not exist."""
    lower = stderr.lower()
    return any(kw in lower for kw in ("not found", "does not exist", "no dhcp scope", "cannot find"))



def run_ps(command: str, parse_json: bool = True) -> dict | list | None:
    """Execute a PowerShell command and optionally parse JSON output.

    Always appends -ErrorAction Stop so errors raise PowerShellError instead
    of silently returning empty output.

    Execution-layer guard: validates DHCP environment before every call.
    This is a mandatory safety net — even if route-level protection is bypassed,
    DHCP operations will not proceed in unsupported environments.
    The validation result is cached so this check is free after the first call.

    Raises:
        DhcpEnvironmentError: if the runtime cannot support DHCP automation.
        PowerShellError: if the PowerShell command exits with a non-zero code.
    """
    dhcp_service.validate_dhcp_environment()

    full_cmd = f"{command} -ErrorAction Stop"
    if parse_json:
        full_cmd += " | ConvertTo-Json -Depth 5 -Compress"

    logger.info("PS> %s", _redact_secrets(command))

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", full_cmd],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise PowerShellError(command, "PowerShell command timed out after 60 seconds", -1)

    if result.returncode != 0:
        logger.error("PS FAILED (rc=%d): %s", result.returncode, result.stderr.strip())
        raise PowerShellError(command, result.stderr.strip(), result.returncode)

    logger.debug("PS OUT: %s", result.stdout.strip()[:500])

    if not parse_json or not result.stdout.strip():
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PowerShellError(
            command,
            f"PowerShell returned non-JSON output: {exc}. stdout={result.stdout.strip()[:200]!r}",
            0,
        ) from exc
