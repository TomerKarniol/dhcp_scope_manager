import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


def verify_token(authorization: str = Header(default="")) -> None:
    if not settings.DHCP_API_TOKEN:
        return

    expected = f"Bearer {settings.DHCP_API_TOKEN}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
