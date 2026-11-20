"""The rules content: classes, monsters, spells, items, and the printed tables."""

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(prefix="/rules", tags=["rules"])
