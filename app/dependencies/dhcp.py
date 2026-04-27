from __future__ import annotations

from app.services import dhcp_service


async def require_dhcp_service() -> None:
    await dhcp_service.validate_dhcp_environment()
