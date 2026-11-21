import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bxapi.app import create_app
from bxapi.request_ids import REQUEST_ID_HEADER
from bxapi.settings import Settings

from .conftest import REFEREE_KEY, bearer

DRAWN = re.compile(r"req_[A-Za-z0-9_-]{22}")


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/health", 200),
        ("GET", "/openapi.json", 200),
        ("GET", "/v1/meta", 200),
        ("POST", "/v1/ability-rolls", 201),
        ("DELETE", "/v1/characters/chr_nope", 404),
        ("GET", "/v1/nope", 404),
        ("GET", "/nope", 404),
        ("PUT", "/v1/meta", 405),
        ("POST", "/v1/dice/roll", 422),
    ],
)
def test_every_response_has_a_request_id(client: TestClient, method: str, path: str, status: int) -> None:
    response = client.request(method, path)
    assert response.status_code == status
    assert DRAWN.fullmatch(response.headers[REQUEST_ID_HEADER])


def test_two_requests_get_different_ids(client: TestClient) -> None:
    ids = {client.get("/health").headers[REQUEST_ID_HEADER] for _ in range(20)}
    assert len(ids) == 20


@pytest.mark.parametrize("sent", ["trace-42", "a", "A.b_c~d-0", "x" * 128])
def test_the_server_uses_the_id_a_client_sends(client: TestClient, sent: str) -> None:
    response = client.get("/v1/sessions/ses_nope", headers={REQUEST_ID_HEADER: sent})
    assert response.headers[REQUEST_ID_HEADER] == sent
    assert response.json()["request_id"] == sent


@pytest.mark.parametrize("sent", [b"", b"x" * 129, b"two words", b"semi;colon", b'quote"d', "caf\u00e9".encode()])
def test_the_server_replaces_an_id_it_does_not_accept(client: TestClient, sent: bytes) -> None:
    response = client.get("/v1/sessions/ses_nope", headers={REQUEST_ID_HEADER.encode(): sent})
    assert DRAWN.fullmatch(response.headers[REQUEST_ID_HEADER])
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_a_problem_from_each_layer_has_the_id_of_its_response(player: TestClient, keyed_settings: Settings) -> None:
    small = keyed_settings.model_copy(update={"max_body_bytes": 64})
    with TestClient(create_app(small), headers=bearer(REFEREE_KEY)) as limited:
        responses = {
            "unauthorized": player.get("/v1/meta", headers={"Authorization": "Bearer wrong-key-0123456789"}),
            "referee_required": player.get("/v1/sessions/ses_nope/referee-view"),
            "not_found": player.get("/v1/sessions/ses_nope"),
            "validation_error": player.post("/v1/dice/roll", json={}),
            "content_invalid": player.post("/v1/dice/roll", json={"expression": "nope"}),
            "method_not_allowed": player.put("/v1/meta"),
            "content_too_large": limited.post("/v1/dice/roll", content=b" " * 65),
        }
    for code, response in responses.items():
        body = response.json()
        assert body["code"] == code
        assert response.headers.get_list(REQUEST_ID_HEADER) == [body["request_id"]], code


def _failing_app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/v1/bug")
    def bug() -> None:
        raise RuntimeError("SECRET")

    return app


def test_an_unexpected_error_has_the_id_in_the_response_and_in_the_log(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        TestClient(_failing_app(settings), raise_server_exceptions=False) as client,
        caplog.at_level(logging.INFO, logger="bxapi"),
    ):
        response = client.get("/v1/bug", headers={REQUEST_ID_HEADER: "trace-500"})
    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "trace-500"
    assert response.json()["request_id"] == "trace-500"
    traceback = next(record for record in caplog.records if record.name == "bxapi" and record.exc_info)
    assert "trace-500" in traceback.getMessage()
    access = [record.getMessage() for record in caplog.records if record.name == "bxapi.access"]
    assert access == [
        f"request_id=trace-500 method=GET path=/v1/bug status=500 duration_ms={_duration(access[0])} client=local"
    ]


def _duration(line: str) -> str:
    found = re.search(r"duration_ms=(\d+\.\d)", line)
    assert found is not None, line
    return found.group(1)


def test_the_server_logs_one_line_for_each_request(player: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="bxapi.access"):
        player.get("/v1/rules/monsters?q=goblin", headers={REQUEST_ID_HEADER: "one"})
        player.get("/v1/meta", headers={REQUEST_ID_HEADER: "two", "Authorization": "Bearer wrong-key-0123456789"})
        player.get("/health", headers={REQUEST_ID_HEADER: "three"})
        player.get("/v1/a%20b%0A", headers={REQUEST_ID_HEADER: "four"})
    lines = [record.getMessage() for record in caplog.records if record.name == "bxapi.access"]
    assert [re.sub(r"duration_ms=\d+\.\d", "duration_ms=N", line) for line in lines] == [
        "request_id=one method=GET path=/v1/rules/monsters status=200 duration_ms=N client=game",
        "request_id=two method=GET path=/v1/meta status=401 duration_ms=N client=-",
        "request_id=three method=GET path=/health status=200 duration_ms=N client=-",
        "request_id=four method=GET path=/v1/a%20b%0A status=404 duration_ms=N client=game",
    ]
    assert not any("key" in line or "goblin" in line for line in lines)


def test_a_page_from_another_origin_can_read_the_id(keyed_settings: Settings) -> None:
    settings = keyed_settings.model_copy(update={"cors_origins": ("https://game.example",)})
    with TestClient(create_app(settings), headers=bearer(REFEREE_KEY)) as client:
        response = client.get("/v1/meta", headers={"Origin": "https://game.example"})
        preflight = client.options(
            "/v1/meta",
            headers={"Origin": "https://game.example", "Access-Control-Request-Method": "GET", "Authorization": ""},
        )
    assert REQUEST_ID_HEADER in response.headers["access-control-expose-headers"]
    assert DRAWN.fullmatch(preflight.headers[REQUEST_ID_HEADER])


def test_the_openapi_document_lists_the_header_on_every_response(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    assert document["components"]["headers"][REQUEST_ID_HEADER]["schema"] == {"type": "string"}
    responses = [
        response
        for path in document["paths"].values()
        for operation in path.values()
        for response in operation["responses"].values()
    ]
    assert len(responses) > 100
    for response in responses:
        assert response["headers"][REQUEST_ID_HEADER] == {"$ref": "#/components/headers/X-Request-Id"}
    assert "request_id" in document["components"]["schemas"]["Problem"]["properties"]
