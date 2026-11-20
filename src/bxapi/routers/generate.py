"""Characters, treasure, and encounters rolled without a game."""

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(prefix="/generate", tags=["generate"])
