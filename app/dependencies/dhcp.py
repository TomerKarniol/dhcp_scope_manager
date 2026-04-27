from __future__ import annotations

from app.services import dhcp_service


def require_dhcp_service() -> None:
    dhcp_service.validate_dhcp_environment()
