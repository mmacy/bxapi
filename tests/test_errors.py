from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from osrlib.errors import ContentValidationError, ReplayVersionError, SaveVersionError
from pydantic import BaseModel

from bxapi.app import create_app
from bxapi.errors import PROBLEM_MEDIA_TYPE, conflict, forbidden, invalid, not_found
from bxapi.settings import Settings


class _Body(BaseModel):
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
        }
        raise errors[kind]

    @app.post("/echo")
    def echo(body: _Body) -> _Body:
        return body

    return app


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
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
    assert api.get("/raise/not-found").json() | {"title": "", "detail": ""} == {
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
    assert "secret" not in api.get("/raise/bug").text


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
    assert response.json()["code"] == "content_too_large"


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
    assert response.json()["code"] == "content_too_large"


def test_cors_headers_only_for_configured_origins(settings: Settings, make_client: Callable[..., TestClient]) -> None:
    allowed = "https://game.example"
    client = make_client(settings.model_copy(update={"cors_origins": (allowed,)}))
    assert client.get("/health", headers={"Origin": allowed}).headers["access-control-allow-origin"] == allowed
    assert "access-control-allow-origin" not in client.get("/health", headers={"Origin": "https://x.example"}).headers
    assert (
        "access-control-allow-origin" not in make_client(settings).get("/health", headers={"Origin": allowed}).headers
    )
