"""Games in progress: commands, views, and events."""

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(prefix="/sessions", tags=["sessions"])
