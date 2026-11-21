import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bxapi.__main__ import main
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


def _run_main_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **environment: str) -> SystemExit:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BXAPI_DATABASE", str(tmp_path / "bxapi.db"))
    monkeypatch.setenv("BXAPI_ALLOW_ANONYMOUS", "true")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as exited:
        main()
    return exited.value


def test_a_bad_setting_names_its_environment_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exited = _run_main_with(monkeypatch, tmp_path, BXAPI_SESSION_CACHE_SIZE="0")
    assert exited.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "bxapi: BXAPI_SESSION_CACHE_SIZE: Input should be greater than or equal to 1\n"
    assert not (tmp_path / "bxapi.db").exists()


def test_a_short_key_is_reported_by_entry_and_field_without_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = '[{"client": "ok", "key": "0123456789abcdef"}, {"client": "short", "key": "s3cret"}]'
    exited = _run_main_with(monkeypatch, tmp_path, BXAPI_API_KEYS=keys)
    assert exited.code == 1
    err = capsys.readouterr().err
    assert err == "bxapi: BXAPI_API_KEYS, entry 2, key: the key must be at least 16 characters long\n"
    assert "s3cret" not in err


def test_every_bad_setting_gets_its_own_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exited = _run_main_with(monkeypatch, tmp_path, BXAPI_PORT="99999", BXAPI_MAX_BODY_BYTES="5")
    assert exited.code == 1
    assert capsys.readouterr().err.splitlines() == [
        "bxapi: BXAPI_MAX_BODY_BYTES: Input should be greater than or equal to 1024",
        "bxapi: BXAPI_PORT: Input should be less than or equal to 65535",
    ]


def test_a_problem_with_no_location_is_printed_without_a_variable_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = '[{"client": "a", "key": "0123456789abcdef"}, {"client": "b", "key": "0123456789abcdef"}]'
    exited = _run_main_with(monkeypatch, tmp_path, BXAPI_API_KEYS=keys)
    assert exited.code == 1
    err = capsys.readouterr().err
    assert err == "bxapi: two entries in BXAPI_API_KEYS have the same key\n"
    assert "0123456789abcdef" not in err


def test_a_server_with_no_keys_exits_with_its_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exited = _run_main_with(monkeypatch, tmp_path, BXAPI_ALLOW_ANONYMOUS="false")
    assert isinstance(exited.code, str)
    assert exited.code.startswith("bxapi: ")
    assert "BXAPI_ALLOW_ANONYMOUS=true" in exited.code


@pytest.mark.parametrize("variable", ["BXAPI_API_KEYS", "BXAPI_CORS_ORIGINS"])
@pytest.mark.parametrize("value", ["not json", "", "[1, 2", "{'client': 'x'}"])
def test_a_list_that_is_not_json_gets_one_line_naming_both_list_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], variable: str, value: str
) -> None:
    exited = _run_main_with(monkeypatch, tmp_path, **{variable: value})
    assert exited.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    [line] = captured.err.splitlines()
    assert line.startswith("bxapi: a BXAPI_ variable that takes a list isn't valid JSON")
    assert 'BXAPI_API_KEYS takes a JSON list, like \'[{"client": "my-game"' in line
    assert "BXAPI_CORS_ORIGINS takes a JSON list, like '[\"https://game.example\"]'" in line
    assert "Traceback" not in captured.err
    if value:
        assert value not in captured.err
    assert not (tmp_path / "bxapi.db").exists()
