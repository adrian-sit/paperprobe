from fastapi import APIRouter

from app.api.routes import health, papers

api_router = APIRouter()
api_router.include_router(health.router, tags=["system"])
api_router.include_router(papers.router, prefix="/papers", tags=["papers"])
