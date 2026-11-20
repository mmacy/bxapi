"""Stored adventures and the adventures that come with the server."""

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(prefix="/adventures", tags=["adventures"])
