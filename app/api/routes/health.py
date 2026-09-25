from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings

router = APIRouter()


@router.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """Liveness endpoint that deliberately exposes no secrets."""
    return {"status": "ok", "environment": settings.environment}


@router.get("/readiness")
async def readiness(settings: Settings = Depends(get_settings)) -> dict[str, bool]:
    """Shows which optional services have local configuration."""
    return {
        "gemini_configured": bool(settings.gemini_api_key),
        "postgres_configured": bool(settings.postgres_dsn),
        "mongodb_configured": bool(settings.mongodb_uri),
    }
