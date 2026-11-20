"""Shared fixtures: settings that point at a temporary database, and clients for each role."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bxapi.app import create_app
from bxapi.settings import ApiKey, Role, Settings

PLAYER_KEY = "player-key-0123456789"
REFEREE_KEY = "referee-key-0123456789"
OTHER_KEY = "other-key-0123456789"

API_KEYS = (
    ApiKey.model_validate({"client": "game", "key": PLAYER_KEY, "role": Role.PLAYER}),
    ApiKey.model_validate({"client": "game", "key": REFEREE_KEY, "role": Role.REFEREE}),
    ApiKey.model_validate({"client": "other", "key": OTHER_KEY, "role": Role.REFEREE}),
)


def bearer(key: str) -> dict[str, str]:
    """Return the Authorization header for an API key."""
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with no API keys and a database file in the test's temporary directory."""
    return Settings(database=str(tmp_path / "bxapi.db"), _env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.fixture
def make_client() -> Iterator[Callable[..., TestClient]]:
    """Build a client for an application with the given settings, and close it after the test."""
    clients: list[TestClient] = []

    def _make(settings: Settings, key: str | None = None) -> TestClient:
        client = TestClient(create_app(settings), headers=bearer(key) if key else None)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(settings: Settings, make_client: Callable[..., TestClient]) -> TestClient:
    """A client for a server with no API keys: every request is a referee for the client `local`."""
    return make_client(settings)


@pytest.fixture
def keyed_settings(settings: Settings) -> Settings:
    """Settings with three API keys: a player and a referee for `game`, and a referee for `other`."""
    return settings.model_copy(update={"api_keys": API_KEYS})


@pytest.fixture
def player(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> TestClient:
    """A client that sends the player key of the client `game`."""
    return make_client(keyed_settings, PLAYER_KEY)


@pytest.fixture
def referee(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> TestClient:
    """A client that sends the referee key of the client `game`."""
    return make_client(keyed_settings, REFEREE_KEY)


@pytest.fixture
def other(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> TestClient:
    """A client that sends the referee key of the client `other`."""
    return make_client(keyed_settings, OTHER_KEY)
