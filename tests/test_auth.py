from collections.abc import Callable

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bxapi.auth import RefereePrincipal
from bxapi.errors import PROBLEM_MEDIA_TYPE, install_error_handlers
from bxapi.settings import Settings

from .conftest import PLAYER_KEY, REFEREE_KEY, bearer


def test_missing_key_is_401(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    response = make_client(keyed_settings).get("/v1/meta")
    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthorized"


@pytest.mark.parametrize("header", ["Bearer wrong-key-0123456789", "Basic abc", "Bearer", PLAYER_KEY])
def test_bad_key_is_401(keyed_settings: Settings, make_client: Callable[..., TestClient], header: str) -> None:
    response = make_client(keyed_settings).get("/v1/meta", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_referee_endpoint_refuses_a_player(keyed_settings: Settings) -> None:
    app = FastAPI()
    app.state.settings = keyed_settings
    install_error_handlers(app)
    router = APIRouter()

    @router.get("/secret")
    def secret(principal: RefereePrincipal) -> dict[str, str]:
        return {"client": principal.client}

    app.include_router(router)
    client = TestClient(app)
    refused = client.get("/secret", headers=bearer(PLAYER_KEY))
    assert refused.status_code == 403
    assert refused.json()["code"] == "referee_required"
    assert client.get("/secret", headers=bearer(REFEREE_KEY)).json() == {"client": "game"}


def test_duplicate_keys_are_rejected() -> None:
    entry = {"client": "a", "key": "k" * 16, "role": "player"}
    with pytest.raises(ValidationError, match="same key"):
        Settings(api_keys=[entry, entry | {"client": "b"}], _env_file=None)  # pyright: ignore[reportCallIssue, reportArgumentType]


def test_short_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(api_keys=[{"client": "a", "key": "short"}], _env_file=None)  # pyright: ignore[reportCallIssue, reportArgumentType]


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BXAPI_PORT", "9000")
    monkeypatch.setenv("BXAPI_API_KEYS", '[{"client": "env", "key": "0123456789abcdef", "role": "referee"}]')
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.port == 9000
    assert settings.api_keys[0].client == "env"
    assert settings.auth_enabled
