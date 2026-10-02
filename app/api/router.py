from fastapi import APIRouter

from app.api.routes import health, versioned_papers

api_router = APIRouter()
api_router.include_router(health.router, tags=["system"])
api_router.include_router(versioned_papers.router, prefix="/papers", tags=["papers"])
