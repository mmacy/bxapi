"""The API's routers, one module per group of endpoints.

[`create_app`][bxapi.app.create_app] mounts every router in
[`V1_ROUTERS`][bxapi.routers.V1_ROUTERS] under `/v1`, behind the API key check. To add an
endpoint, add a route to the module for its group.
"""

from fastapi import APIRouter

from bxapi.routers import adventures, characters, dice, generate, meta, rules, saves, sessions

__all__ = ["V1_ROUTERS"]

V1_ROUTERS: tuple[APIRouter, ...] = (
    meta.router,
    rules.router,
    dice.router,
    generate.router,
    characters.router,
    adventures.router,
    sessions.router,
    saves.router,
)
"""The routers mounted under `/v1`, in the order their groups appear in the OpenAPI document."""
