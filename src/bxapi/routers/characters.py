"""Stored characters, and the ability score rolls that start one."""

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(tags=["characters"])
