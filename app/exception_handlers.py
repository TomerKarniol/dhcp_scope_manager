import logging
import re
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from app.services.dhcp_env import DhcpEnvironmentError
from app.services.ps_executor import PowerShellError
from app.utils.exceptions import DhcpApiError

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

    @app.exception_handler(DhcpApiError)
    async def domain_error_handler(request: Request, exc: DhcpApiError) -> JSONResponse:
        # Catches domain exceptions raised from dependency functions, which execute
        # before the route handler and are therefore outside the handle_http_errors decorator.
        logger.warning(
            "Domain error on %s %s: [%d] %s",
            request.method,
            request.url.path,
            exc.http_status,
            exc.detail,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={"detail": exc.detail},
        )

    """Register all global exception handlers on the FastAPI app.

    Handles:
    - DhcpEnvironmentError → HTTP 503 with reason + detail fields
      Raised when the runtime does not support DHCP automation (wrong OS, missing
      PowerShell, missing DHCP cmdlets).  503 is appropriate: the server exists
      but cannot currently service DHCP requests.

    - PowerShellError → HTTP 500 with ps_error field
      Raised when a PowerShell cmdlet exits non-zero during a DHCP operation.
    """

    @app.exception_handler(DhcpEnvironmentError)
    async def dhcp_env_error_handler(
        request: Request, exc: DhcpEnvironmentError
    ) -> JSONResponse:
        """
        Converts DhcpEnvironmentError into HTTP 503.

        Response body:
        - ``detail``: human-readable description (suitable for operator logs/alerts)
        - ``reason``: machine-readable code from DhcpEnvReason (suitable for tooling)
        """
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
        """
        Converts unhandled PowerShellError into HTTP 500.

        Response body:
        - ``detail``: human-readable error message
        - ``ps_error``: raw stderr from PowerShell (useful for diagnosing DHCP server issues)
        """
        logger.error(
            "PowerShell error on %s %s: %s",
            request.method,
            request.url.path,
            exc.stderr,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": str(exc), "ps_error": _sanitize_ps_stderr(exc.stderr)},
        )
