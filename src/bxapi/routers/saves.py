"""Saved games."""

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(prefix="/saves", tags=["saves"])
