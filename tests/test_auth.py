import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bxapi.app import create_app
from bxapi.auth import RefereePrincipal
from bxapi.errors import PROBLEM_MEDIA_TYPE, install_error_handlers
from bxapi.settings import ApiKey, Settings, SettingsError

from .conftest import PLAYER_KEY, REFEREE_KEY, bearer


def test_missing_key_is_401(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    response = make_client(keyed_settings).get("/v1/meta")
    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["code"] == "unauthorized"


@pytest.mark.parametrize(
    "header",
    [
        "Bearer wrong-key-0123456789",
        "Basic abc",
        "Bearer",
        "Bearer ",
        PLAYER_KEY,
        f"Bearer {PLAYER_KEY}x",
        f"Bearer {PLAYER_KEY[:-1]}",
    ],
)
def test_bad_key_is_401(keyed_settings: Settings, make_client: Callable[..., TestClient], header: str) -> None:
    response = make_client(keyed_settings).get("/v1/meta", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_non_ascii_token_is_401(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    client = make_client(keyed_settings)
    for token in ("Bearer ключ-0123456789abcdef".encode(), b"Bearer \xff\xfe-0123456789abcdef"):
        response = client.get("/v1/meta", headers=[(b"Authorization", token)])
        assert response.status_code == 401
        assert response.json()["code"] == "unauthorized"


def test_the_scheme_is_case_insensitive(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    client = make_client(keyed_settings)
    for scheme in ("bearer", "BEARER", "Bearer"):
        response = client.get("/v1/meta", headers={"Authorization": f"{scheme} {PLAYER_KEY}"})
        assert response.status_code == 200
        assert response.json()["role"] == "player"


def test_no_key_and_a_malformed_body_is_401(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    client = make_client(keyed_settings)
    response = client.post("/v1/dice/roll", content=b'{"expression": ', headers={"Content-Type": "application/json"})
    assert response.status_code == 401
    big = b'{"expression": "1d6", "pad": "' + b"x" * (9 * 1024 * 1024) + b'"}'
    assert client.post("/v1/dice/roll", content=big).status_code == 401


def test_a_path_with_no_route_under_v1_needs_a_key(
    keyed_settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    assert make_client(keyed_settings).get("/v1/nowhere").status_code == 401
    assert make_client(keyed_settings, PLAYER_KEY).get("/v1/nowhere").status_code == 404


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
    assert client.get("/secret").status_code == 401
    refused = client.get("/secret", headers=bearer(PLAYER_KEY))
    assert refused.status_code == 403
    assert refused.json()["code"] == "referee_required"
    assert client.get("/secret", headers=bearer(REFEREE_KEY)).json() == {"client": "game"}


def test_a_server_with_no_keys_refuses_to_start(tmp_path: Path) -> None:
    settings = Settings(database=str(tmp_path / "bxapi.db"), _env_file=None)  # pyright: ignore[reportCallIssue]
    with pytest.raises(SettingsError) as refused:
        create_app(settings)
    assert "BXAPI_API_KEYS" in str(refused.value)
    assert "BXAPI_ALLOW_ANONYMOUS=true" in str(refused.value)
    assert not (tmp_path / "bxapi.db").exists()


def test_allow_anonymous_has_no_effect_with_keys(
    keyed_settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    assert keyed_settings.allow_anonymous
    assert make_client(keyed_settings).get("/v1/meta").status_code == 401


def test_startup_logs_a_warning_when_anonymous(
    settings: Settings, make_client: Callable[..., TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bxapi")
    make_client(settings.model_copy(update={"cors_origins": ("https://game.example",)}))
    [record] = [record for record in caplog.records if record.name == "bxapi"]
    assert record.levelno == logging.WARNING
    assert "Keys: 0." in record.getMessage()
    assert "https://game.example" in record.getMessage()


def test_startup_logs_the_clients_and_never_the_keys(
    keyed_settings: Settings, make_client: Callable[..., TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="bxapi")
    make_client(keyed_settings)
    [record] = [record for record in caplog.records if record.name == "bxapi"]
    message = record.getMessage()
    assert record.levelno == logging.INFO
    assert "Keys: 3. Clients: game, other. CORS origins: none." in message
    assert PLAYER_KEY not in message and REFEREE_KEY not in message


def test_duplicate_keys_are_rejected() -> None:
    entry = ApiKey.model_validate({"client": "a", "key": "k" * 16, "role": "player"})
    with pytest.raises(ValidationError, match="same key"):
        Settings(api_keys=(entry, entry.model_copy(update={"client": "b"})), _env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    "key",
    [
        "s3cr3t-short",
        "s3cr3t has spaces 0123",
        "s3cr3t\ttab-0123456789",
        "s3cr3t-ключ-0123456789",
        "s3cr3t-0123456789\n",
    ],
)
def test_bad_keys_are_rejected_without_repeating_them(key: str) -> None:
    with pytest.raises(ValidationError) as refused:
        ApiKey.model_validate({"client": "a", "key": key})
    assert "s3cr3t" not in str(refused.value)


def test_a_bad_key_in_the_environment_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BXAPI_API_KEYS", '[{"client": "env", "key": "secret with spaces 123"}]')
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert "secret with spaces" not in str(refused.value)


def test_an_unknown_key_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="rol"):
        ApiKey.model_validate({"client": "a", "key": "0123456789abcdef", "rol": "referee"})


@pytest.mark.parametrize(
    "origin", ["*", "https://game.example/", "https://game.example/play", "game.example", "https://", "https://a:99999"]
)
def test_malformed_cors_origins_are_rejected(origin: str) -> None:
    with pytest.raises(ValidationError, match="CORS origin"):
        Settings(cors_origins=(origin,), _env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize("origin", ["https://game.example", "http://localhost:3000", "http://[::1]:8000"])
def test_cors_origins_are_accepted(origin: str) -> None:
    assert Settings(cors_origins=(origin,), _env_file=None).cors_origins == (origin,)  # pyright: ignore[reportCallIssue]


def test_settings_read_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BXAPI_PORT", "9000")
    monkeypatch.setenv("BXAPI_API_KEYS", '[{"client": "env", "key": "0123456789abcdef", "role": "referee"}]')
    monkeypatch.setenv("BXAPI_BODY_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("BXAPI_DOCS_ENABLED", "false")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.port == 9000
    assert settings.api_keys[0].client == "env"
    assert settings.auth_enabled
    assert not settings.allow_anonymous
    assert settings.body_timeout_seconds == 2.5
    assert not settings.docs_enabled


def test_the_tests_see_no_bxapi_variables() -> None:
    assert not [name for name in os.environ if name.startswith("BXAPI_")]
