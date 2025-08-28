"""API dependencies for authentication and authorization."""

from typing import Annotated
from fastapi import Header, HTTPException, status
from src.infrastructure.config import get_settings

settings = get_settings()


async def verify_api_key(
    x_api_key: Annotated[str, Header()] = None
) -> str:
    """Verify the API key from request headers.
    
    Args:
        x_api_key: API key from X-API-Key header
        
    Returns:
        The verified API key
        
    Raises:
        HTTPException: If API key is missing or invalid
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    
    if x_api_key != settings.service_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    
    return x_api_key