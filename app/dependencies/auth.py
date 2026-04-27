import hmac

from fastapi import Header

from app.config import settings
from app.errors import UnauthorizedError


async def verify_token(authorization: str = Header(default="")) -> None:
    if not settings.DHCP_API_TOKEN:
        return

    expected = f"Bearer {settings.DHCP_API_TOKEN}"
    if not hmac.compare_digest(authorization, expected):
        raise UnauthorizedError()
