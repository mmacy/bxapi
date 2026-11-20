"""Dice rolls."""

from fastapi import APIRouter

__all__ = ["router"]

router = APIRouter(prefix="/dice", tags=["dice"])
