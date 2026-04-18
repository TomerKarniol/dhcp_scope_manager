from fastapi import APIRouter, Depends, status

from app.dependencies.auth import verify_token
from app.services import dhcp_service
from app.utils.decorators import handle_health_errors

router = APIRouter(tags=["health"])


@router.get("/healthz", status_code=status.HTTP_200_OK)
@handle_health_errors
def healthz(_: None = Depends(verify_token)):
    return dhcp_service.check_health()
