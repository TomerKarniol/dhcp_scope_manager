from __future__ import annotations
import os
import sys

# Allow running directly from project root or from inside app/:
#   python app/main.py   (from project root)
#   python main.py       (from inside app/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from app.config import settings
from app.logging_config import configure_logging
from app.exception_handlers import register_exception_handlers
from app.routers import router

configure_logging(settings.LOG_LEVEL)

app = FastAPI(
    title="DHCP Scope Management API",
    version="1.0.0",
    description=(
        "Manages Windows DHCP scopes via PowerShell cmdlets. "
        "Consumed exclusively by Crossplane provider-http."
    ),
)

app.include_router(router)

register_exception_handlers(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        reload=False,
    )
