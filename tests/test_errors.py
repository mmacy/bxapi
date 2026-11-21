import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from osrlib.errors import ContentValidationError, ReplayVersionError, SaveVersionError
from pydantic import BaseModel, ConfigDict
from starlette.types import Message, Receive, Scope

from bxapi.app import create_app
from bxapi.errors import (
    PROBLEM_MEDIA_TYPE,
    conflict,
    content_too_large,
    database_busy,
    forbidden,
    internal_error,
    invalid,
    not_found,
    request_timeout,
)
from bxapi.settings import Settings

from .conftest import LOCAL_URL, PLAYER_KEY, bearer, problem_without_id


class _Body(BaseModel):
    count: int


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/raise/{kind}")
    def raise_error(kind: str) -> None:
        errors = {
            "not-found": not_found("session", "ses_x"),
            "forbidden": forbidden("referee_required", "No."),
            "conflict": conflict("revision_conflict", "Moved on.", revision=3),
            "invalid": invalid("character_invalid", "Bad.", rejections=[{"code": "a.b", "params": {}}]),
            "content": ContentValidationError("malformed"),
            "save-version": SaveVersionError("too new"),
            "replay-version": ReplayVersionError("other engine"),
            "bug": RuntimeError("secret internals"),
            "long-content": ContentValidationError("x" * 5_000_000),
            "long-version": SaveVersionError("y" * 5_000_000),
            "sql-bug": sqlite3.OperationalError("no such table: secret_table"),
        }
        raise errors[kind]

    @app.post("/echo")
    def echo(body: _Body) -> _Body:
        return body

    @app.post("/strict")
    def strict(body: _StrictBody) -> _StrictBody:
        return body

    return app


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=LOCAL_URL, raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [
        ("not-found", 404, "not_found"),
        ("forbidden", 403, "referee_required"),
        ("conflict", 409, "revision_conflict"),
        ("invalid", 422, "character_invalid"),
        ("content", 422, "content_invalid"),
        ("save-version", 409, "version_unsupported"),
        ("replay-version", 409, "version_unsupported"),
        ("bug", 500, "internal_error"),
    ],
)
def test_every_error_is_a_problem_document(api: TestClient, kind: str, status: int, code: str) -> None:
    response = api.get(f"/raise/{kind}")
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    assert body["title"] and body["detail"]


def test_extra_members_are_sent(api: TestClient) -> None:
    assert problem_without_id(api.get("/raise/not-found")) | {"title": "", "detail": ""} == {
        "type": "about:blank",
        "title": "",
        "status": 404,
        "detail": "",
        "code": "not_found",
        "resource": "session",
        "id": "ses_x",
    }
    assert api.get("/raise/conflict").json()["revision"] == 3


def test_unexpected_errors_hide_the_cause(api: TestClient) -> None:
    response = api.get("/raise/bug")
    assert "secret" not in response.text
    assert problem_without_id(response) == internal_error().problem().model_dump(mode="json")


def test_request_validation_lists_each_field(api: TestClient) -> None:
    response = api.post("/echo", json={"count": "many"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"][0]["loc"] == ["body", "count"]


def test_unknown_path_and_method_are_problem_documents(api: TestClient) -> None:
    missing = api.get("/v1/nowhere")
    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
    wrong_method = api.delete("/health")
    assert (wrong_method.status_code, wrong_method.json()["code"]) == (405, "method_not_allowed")


def test_declared_body_over_the_limit_is_413(settings: Settings, make_client: Callable[..., TestClient]) -> None:
    client = make_client(settings.model_copy(update={"max_body_bytes": 1024}))
    response = client.post("/v1/meta", content=b"x" * 2048)
    assert response.status_code == 413
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert problem_without_id(response) == content_too_large(1024).problem().model_dump(mode="json")


def test_streamed_body_over_the_limit_is_413(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"max_body_bytes": 1024}))

    @app.post("/echo")
    def echo(body: _Body) -> _Body:
        return body

    def chunks() -> Iterator[bytes]:
        yield b'{"count": 1, "pad": "'
        for _ in range(4):
            yield b"x" * 512
        yield b'"}'

    with TestClient(app) as client:
        response = client.post("/echo", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert problem_without_id(response) == content_too_large(1024).problem().model_dump(mode="json")


def test_cors_headers_only_for_configured_origins(settings: Settings, make_client: Callable[..., TestClient]) -> None:
    allowed = "https://game.example"
    client = make_client(settings.model_copy(update={"cors_origins": (allowed,)}))
    assert client.get("/health", headers={"Origin": allowed}).headers["access-control-allow-origin"] == allowed
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": "https://x.example"}).headers
    assert (
        "access-control-allow-origin" not in make_client(settings).get("/health", headers={"Origin": allowed}).headers
    )


def test_not_found_repeats_at_most_128_characters_of_the_id() -> None:
    problem = not_found("monster", "x" * 5000).problem().model_dump()
    assert problem["id"] == "x" * 128 + "..."
    assert len(problem["detail"]) < 200
    assert not_found("monster", "goblin").problem().model_dump()["id"] == "goblin"


def test_validation_errors_are_capped(api: TestClient) -> None:
    long_name = "n" * 5000
    body = {f"field{n}": "secret-value" for n in range(5000)} | {long_name: 1, "count": "many"}
    response = api.post("/strict", json=body)
    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "validation_error"
    assert problem["error_count"] == 5002
    assert len(problem["errors"]) == 20
    assert len(response.content) < 10_000
    assert "secret-value" not in response.text
    assert all(set(entry) == {"loc", "msg", "type"} for entry in problem["errors"])
    assert all(len(part) <= 128 for entry in problem["errors"] for part in entry["loc"] if isinstance(part, str))
    assert all(len(entry["msg"]) <= 128 for entry in problem["errors"])


def test_a_long_location_is_shortened(api: TestClient) -> None:
    response = api.post("/strict", json={"count": 1, "k" * 5000: 1})
    [entry] = response.json()["errors"]
    assert entry["loc"][0] == "body"
    assert entry["loc"][1] == "k" * 125 + "..."


def test_a_body_that_cannot_be_decoded_is_400(referee: TestClient) -> None:
    headers = {"Content-Type": "application/json"}
    for content in (b'{"expression": "\xff1d6"}', b'{"expression": "1d6", "times": ' + b"9" * 5000 + b"}"):
        response = referee.post("/v1/dice/roll", content=content, headers=headers)
        assert response.status_code == 400
        assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
        assert response.json()["code"] == "bad_request"


@pytest.mark.parametrize(
    ("kind", "code"), [("long-content", "content_invalid"), ("long-version", "version_unsupported")]
)
def test_osrlib_details_are_shortened(api: TestClient, kind: str, code: str) -> None:
    problem = api.get(f"/raise/{kind}").json()
    assert problem["code"] == code
    assert len(problem["detail"]) == 1000
    assert problem["detail"].endswith("...")


def test_another_database_error_is_500(api: TestClient) -> None:
    other = api.get("/raise/sql-bug")
    assert (other.status_code, other.json()["code"]) == (500, "internal_error")
    assert "secret_table" not in other.text


def test_a_write_lock_held_by_another_process_is_503(
    keyed_settings: Settings, make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bxapi.db.BUSY_TIMEOUT_SECONDS", 0.1)
    client = make_client(keyed_settings, PLAYER_KEY)
    holder = sqlite3.connect(keyed_settings.database, autocommit=True)
    holder.execute("BEGIN IMMEDIATE")
    try:
        response = client.post("/v1/ability-rolls")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert problem_without_id(response) == database_busy().problem().model_dump(mode="json")
    assert client.post("/v1/ability-rolls").status_code == 201


def _call(app: FastAPI, scope: Scope, receive: Receive) -> tuple[int, dict[str, Any]]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def run() -> None:
        await app(scope, receive, send)

    asyncio.run(run())
    status = sent[0]["status"]
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, json.loads(body)


def test_a_body_that_never_arrives_is_408(keyed_settings: Settings) -> None:
    app = create_app(keyed_settings.model_copy(update={"body_timeout_seconds": 0.2}))
    headers = [
        (b"authorization", bearer(PLAYER_KEY)["Authorization"].encode()),
        (b"content-type", b"application/json"),
        (b"content-length", b"100"),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/dice/roll",
        "raw_path": b"/v1/dice/roll",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8640),
    }
    first = True

    async def receive() -> Message:
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b'{"expression": ', "more_body": True}
        await asyncio.sleep(60)
        raise AssertionError("the server waited past its body timeout")

    status, problem = _call(app, scope, receive)
    assert status == 408
    assert problem.pop("request_id").startswith("req_")
    assert problem == request_timeout(0.2).problem().model_dump(mode="json")
    app.state.db.close()


def test_cors_preflight_needs_no_key(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    allowed = "https://game.example"
    client = make_client(keyed_settings.model_copy(update={"cors_origins": (allowed,)}))
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization"}
    response = client.options("/v1/dice/roll", headers={"Origin": allowed} | preflight)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == allowed
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    refused = client.options("/v1/dice/roll", headers={"Origin": "https://x.example"} | preflight)
    assert refused.status_code == 400
    assert "access-control-allow-origin" not in refused.headers


def test_cors_exposes_the_authentication_and_retry_headers(
    keyed_settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    allowed = "https://game.example"
    client = make_client(keyed_settings.model_copy(update={"cors_origins": (allowed,)}))
    response = client.get("/v1/meta", headers={"Origin": allowed})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == allowed
    exposed = {name.strip().lower() for name in response.headers["access-control-expose-headers"].split(",")}
    assert {"www-authenticate", "retry-after"} <= exposed


def test_a_body_with_many_unknown_fields_gets_one_error(referee: TestClient) -> None:
    few = referee.post("/v1/dice/roll", json={"expression": "1d6"} | {f"f{n}": 0 for n in range(20)}).json()
    assert few["error_count"] == 20
    assert {entry["type"] for entry in few["errors"]} == {"extra_forbidden"}
    many = referee.post("/v1/dice/roll", json={"expression": "1d6"} | {f"f{n}": 0 for n in range(4_000)}).json()
    assert many["error_count"] == 1
    [entry] = many["errors"]
    assert (entry["loc"], entry["type"]) == (["body"], "extra_forbidden")
    assert "4000" in entry["msg"]
