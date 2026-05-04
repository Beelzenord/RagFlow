from fastapi import Header, HTTPException, status
from .settings import settings


async def require_service_key(x_api_key: str | None = Header(default=None)) -> None:
    """Shared-secret auth between n8n and the FastAPI services. Disabled if SERVICE_API_KEY is empty."""
    expected = settings.service_api_key
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
