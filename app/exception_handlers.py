import logging
import re
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from app.services.dhcp_service import DhcpEnvironmentError
from app.services.ps_executor import PowerShellError

logger = logging.getLogger(__name__)

# Matches Windows-style absolute paths (e.g. C:\Windows\...) to strip from client responses.
_WIN_PATH_RE = re.compile(r'[A-Za-z]:\\[^\s,;]+')
_MAX_PS_ERROR_LEN = 500


def _sanitize_ps_stderr(stderr: str) -> str:
    """Truncate and strip internal path details from PS stderr before returning to clients.

    Raw PowerShell stderr can contain Windows file paths, hostnames, stack traces, and
    policy details that leak infrastructure internals to API consumers.
    Full stderr is preserved in server logs for operator diagnosis.
    """
    sanitized = _WIN_PATH_RE.sub("<path>", stderr)
    return sanitized[:_MAX_PS_ERROR_LEN]


def register_exception_handlers(app: FastAPI) -> None:
    """Register all global exception handlers on the FastAPI app.

    Handles:
    - DhcpEnvironmentError → HTTP 503 with reason + detail fields.
      Raised when the runtime does not support DHCP automation (wrong OS, missing
      PowerShell, missing DHCP cmdlets).
    - PowerShellError    → HTTP 500 with ps_error field (stderr sanitized).
      Raised when a PowerShell cmdlet exits non-zero during a DHCP operation.
    """

    @app.exception_handler(DhcpEnvironmentError)
    async def dhcp_env_error_handler(
        request: Request, exc: DhcpEnvironmentError
    ) -> JSONResponse:
        logger.error(
            "DHCP environment error on %s %s [%s]: %s",
            request.method,
            request.url.path,
            exc.reason,
            exc.detail,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": exc.detail, "reason": exc.reason},
        )

    @app.exception_handler(PowerShellError)
    async def powershell_error_handler(request: Request, exc: PowerShellError) -> JSONResponse:
        logger.error(
            "PowerShell error on %s %s: %s",
            request.method,
            request.url.path,
            exc.stderr,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "PowerShell command failed", "ps_error": _sanitize_ps_stderr(exc.stderr)},
        )
