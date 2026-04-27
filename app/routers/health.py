from fastapi import APIRouter, Depends, status

from app.dependencies.auth import verify_token
from app.services import dhcp_service

router = APIRouter(tags=["health"])


@router.get("/healthz", status_code=status.HTTP_200_OK)
async def healthz(_: None = Depends(verify_token)):
    return await dhcp_service.check_health()
