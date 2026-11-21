import asyncio
import gzip
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message

from bxapi.app import COMPRESSION_MINIMUM_BYTES, create_app
from bxapi.caching import REVALIDATE
from bxapi.routers.rules import CACHE_CONTROL
from bxapi.settings import Settings

from .conftest import REFEREE_KEY, bearer

ETAG = re.compile(r'W/"[0-9a-f]{32}"')
FIGHTER = {"name": "Brenna", "class_id": "fighter", "alignment": "lawful"}
DICE = {"expression": "3d6", "seed": 7}


def start(client: TestClient) -> str:
    character = client.post("/v1/characters", json=FIGHTER).json()
    session = client.post("/v1/sessions", json={"adventure_id": "builtin-barrow", "character_ids": [character["id"]]})
    return session.json()["id"]


# Conditional requests


@pytest.mark.parametrize(
    "path", ["/health", "/openapi.json", "/v1/meta", "/v1/rules/classes", "/v1/characters", "/v1/adventures"]
)
def test_a_get_has_an_etag_and_answers_304_to_it(client: TestClient, path: str) -> None:
    first = client.get(path)
    assert first.status_code == 200
    etag = first.headers["etag"]
    assert ETAG.fullmatch(etag)
    again = client.get(path, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    assert again.headers["x-request-id"] != first.headers["x-request-id"]
    assert "content-type" not in again.headers
    assert client.get(path).headers["etag"] == etag


@pytest.mark.parametrize(
    ("sent", "status"),
    [
        ("{etag}", 304),
        ('"{opaque}"', 304),
        ('"other", {etag}', 304),
        ('W/"other" , "{opaque}"', 304),
        ("*", 304),
        ('"other"', 200),
        ('W/"other", "another"', 200),
        ("{opaque}", 200),
        ("", 200),
    ],
)
def test_if_none_match_compares_weakly_against_each_tag(client: TestClient, sent: str, status: int) -> None:
    etag = client.get("/v1/meta").headers["etag"]
    header = sent.format(etag=etag, opaque=etag.removeprefix('W/"').removesuffix('"'))
    assert client.get("/v1/meta", headers={"If-None-Match": header}).status_code == status


def test_the_etag_changes_when_the_content_does(client: TestClient) -> None:
    session_id = start(client)
    paths = [f"/v1/sessions/{session_id}", f"/v1/sessions/{session_id}/view", f"/v1/sessions/{session_id}/events"]
    before = {path: client.get(path).headers["etag"] for path in paths}
    for path, etag in before.items():
        assert client.get(path, headers={"If-None-Match": etag}).status_code == 304
    refused = client.post(f"/v1/sessions/{session_id}/commands", json={"command_type": "move_party", "direction": "n"})
    assert refused.status_code == 422
    entered = client.post(
        f"/v1/sessions/{session_id}/commands", json={"command_type": "enter_dungeon", "dungeon_id": "barrow"}
    )
    assert entered.json()["accepted"]
    for path, etag in before.items():
        changed = client.get(path, headers={"If-None-Match": etag})
        assert changed.status_code == 200, path
        assert changed.headers["etag"] != etag
        assert changed.json() == client.get(path).json()


def test_only_a_200_to_a_get_has_an_etag(client: TestClient) -> None:
    responses = [
        client.get("/v1/sessions/ses_nope"),
        client.get("/v1/nope"),
        client.post("/v1/dice/roll", json=DICE),
        client.post("/v1/ability-rolls"),
        client.delete("/v1/characters/chr_nope"),
    ]
    assert [response.status_code for response in responses] == [404, 404, 200, 201, 404]
    for response in responses:
        assert "etag" not in response.headers
        assert "cache-control" not in response.headers


def test_a_post_ignores_if_none_match(client: TestClient) -> None:
    response = client.post("/v1/dice/roll", json=DICE, headers={"If-None-Match": "*"})
    assert response.status_code == 200
    assert response.json()["rolls"][0]["total"] == 9


def test_a_request_without_a_key_gets_401_whatever_it_sends(player: TestClient) -> None:
    etag = player.get("/v1/meta").headers["etag"]
    response = player.get("/v1/meta", headers={"If-None-Match": etag, "Authorization": "Bearer wrong-key-0123456789"})
    assert response.status_code == 401
    assert "etag" not in response.headers


def test_two_clients_get_different_etags_for_different_content(player: TestClient, other: TestClient) -> None:
    etag = player.get("/v1/meta").headers["etag"]
    response = other.get("/v1/meta", headers={"If-None-Match": etag})
    assert response.status_code == 200
    assert response.json()["client"] == "other"


def test_cache_control_tells_a_browser_to_revalidate(client: TestClient) -> None:
    session_id = start(client)
    for path in ("/v1/meta", "/v1/characters", f"/v1/sessions/{session_id}/view", f"/v1/sessions/{session_id}"):
        assert client.get(path).headers["cache-control"] == REVALIDATE, path
    etag = client.get("/v1/meta").headers["etag"]
    assert client.get("/v1/meta", headers={"If-None-Match": etag}).headers["cache-control"] == REVALIDATE
    rules = client.get("/v1/rules/classes")
    assert rules.headers["cache-control"] == CACHE_CONTROL
    unchanged = client.get("/v1/rules/classes", headers={"If-None-Match": rules.headers["etag"]})
    assert (unchanged.status_code, unchanged.headers["cache-control"]) == (304, CACHE_CONTROL)
    for path in ("/health", "/openapi.json"):
        assert "cache-control" not in client.get(path).headers, path


# HEAD


@pytest.mark.parametrize("path", ["/health", "/v1/meta", "/v1/rules/monsters?limit=200", "/v1/sessions/ses_nope"])
def test_head_has_the_headers_of_get_and_no_body(client: TestClient, path: str) -> None:
    get = client.get(path)
    head = client.head(path)
    assert head.status_code == get.status_code
    assert head.content == b""
    for name in ("content-type", "content-length", "etag", "cache-control", "content-encoding", "vary"):
        assert head.headers.get(name) == get.headers.get(name), name
    assert head.headers["x-request-id"].startswith("req_")


def test_head_answers_a_conditional_request(client: TestClient) -> None:
    etag = client.get("/v1/meta").headers["etag"]
    assert client.head("/v1/meta", headers={"If-None-Match": etag}).status_code == 304


def test_head_needs_a_key_and_a_route_that_answers_get(player: TestClient) -> None:
    assert player.head("/v1/meta", headers={"Authorization": ""}).status_code == 401
    refused = player.head("/v1/dice/roll")
    assert (refused.status_code, refused.headers["allow"], refused.content) == (405, "POST", b"")


# Compression


def test_a_large_response_is_compressed_for_a_client_that_accepts_gzip(client: TestClient) -> None:
    plain = client.get("/v1/rules/monsters?limit=200", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert len(plain.content) > 40_000
    with client.stream("GET", "/v1/rules/monsters?limit=200", headers={"Accept-Encoding": "gzip"}) as packed:
        raw = b"".join(packed.iter_raw())
    assert packed.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in packed.headers["vary"]
    assert len(raw) < len(plain.content) / 4
    assert gzip.decompress(raw) == plain.content
    assert packed.headers["etag"] == plain.headers["etag"]


def test_a_small_response_is_not_compressed(client: TestClient) -> None:
    response = client.get("/v1/meta", headers={"Accept-Encoding": "gzip"})
    assert len(response.content) < COMPRESSION_MINIMUM_BYTES
    assert "content-encoding" not in response.headers


def test_a_large_post_response_and_a_large_problem_are_compressed(client: TestClient) -> None:
    rolled = client.post("/v1/dice/roll", json={"expression": "3d6", "times": 100}, headers={"Accept-Encoding": "gzip"})
    assert rolled.headers["content-encoding"] == "gzip"
    assert len(rolled.json()["rolls"]) == 100
    body: dict[str, Any] = {f"field{n}": 1 for n in range(20)}
    problem = client.post("/v1/dice/roll", json=body, headers={"Accept-Encoding": "gzip"})
    assert (problem.status_code, problem.headers["content-encoding"]) == (422, "gzip")
    assert problem.json()["code"] == "validation_error"


# JSON bodies


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", "APPLICATION/JSON", "application/merge-patch+json"],
)
def test_a_json_content_type_is_accepted(client: TestClient, content_type: str) -> None:
    response = client.post("/v1/dice/roll", content=json.dumps(DICE), headers={"Content-Type": content_type})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/x-www-form-urlencoded", "text/json", "application/jsonx", "json", "", None],
)
def test_a_body_that_is_not_declared_as_json_is_415(client: TestClient, content_type: str | None) -> None:
    headers = {} if content_type is None else {"Content-Type": content_type}
    for method, path in (("POST", "/v1/dice/roll"), ("POST", "/v1/characters"), ("PATCH", "/v1/sessions/ses_nope")):
        response = client.request(method, path, content=json.dumps(DICE), headers=headers)
        assert response.status_code == 415, (method, path, response.text)
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "unsupported_media_type"
        assert "application/json" in response.json()["detail"]


def test_a_request_with_no_body_needs_no_content_type(client: TestClient) -> None:
    session_id = start(client)
    assert client.post("/v1/ability-rolls").status_code == 201
    assert client.post("/v1/ability-rolls", headers={"Content-Type": "text/plain"}).status_code == 201
    assert client.post(f"/v1/sessions/{session_id}/saves", content=b"").status_code == 201
    assert client.delete(f"/v1/sessions/{session_id}", headers={"Content-Type": "text/plain"}).status_code == 204


def test_the_key_and_the_body_limit_are_checked_before_the_content_type(keyed_settings: Settings) -> None:
    small = keyed_settings.model_copy(update={"max_body_bytes": 1024})
    plain = {"Content-Type": "text/plain"}
    with TestClient(create_app(small), headers=bearer(REFEREE_KEY)) as client:
        no_key = client.post("/v1/dice/roll", content=b"{}", headers={**plain, "Authorization": ""})
        too_large = client.post("/v1/dice/roll", content=b" " * 1025, headers=plain)
    assert (no_key.status_code, too_large.status_code) == (401, 413)


# The OpenAPI document


def test_the_openapi_document_describes_conditional_requests_on_every_get(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    gets = [item["get"] for item in document["paths"].values() if "get" in item]
    assert len(gets) > 20
    for operation in gets:
        assert [parameter["in"] for parameter in operation["parameters"] if parameter["name"] == "If-None-Match"] == [
            "header"
        ]
        assert operation["responses"]["200"]["headers"]["ETag"] == {"$ref": "#/components/headers/ETag"}
        assert "content" not in operation["responses"]["304"]
    posts = [item["post"] for item in document["paths"].values() if "post" in item]
    for operation in posts:
        assert "304" not in operation["responses"]
        assert "unsupported_media_type" in operation["responses"]["415"]["description"]


# What an independent review found


def test_a_304_varies_on_accept_encoding_like_the_compressed_200(client: TestClient) -> None:
    path = "/v1/rules/monsters?limit=200"
    full = client.get(path)
    unchanged = client.get(path, headers={"If-None-Match": full.headers["etag"]})
    assert full.headers["vary"] == unchanged.headers["vary"] == "Accept-Encoding"
    small = client.get("/v1/meta")
    assert client.get("/v1/meta", headers={"If-None-Match": small.headers["etag"]}).headers["vary"] == "Accept-Encoding"


def test_an_empty_chunked_body_needs_no_content_type(client: TestClient) -> None:
    def nothing() -> Iterator[bytes]:
        yield b""

    response = client.post("/v1/ability-rolls", content=nothing())
    assert response.request.headers["transfer-encoding"] == "chunked"
    assert response.status_code == 201, response.text


def test_a_chunked_body_that_is_not_declared_as_json_is_415(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        yield b'{"expression": '
        yield b'"3d6"}'

    assert client.post("/v1/dice/roll", content=chunks()).status_code == 415
    declared = client.post("/v1/dice/roll", content=chunks(), headers={"Content-Type": "application/json"})
    assert declared.status_code == 200


def test_head_has_no_body_when_the_server_fails(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/v1/bug")
    def bug() -> None:
        raise RuntimeError("SECRET")

    def call(method: str) -> tuple[int, dict[bytes, bytes], bytes]:
        sent: list[Message] = []

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/v1/bug",
            "raw_path": b"/v1/bug",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8640),
        }
        # Starlette sends the 500 and then raises the exception again, for the server to log.
        with pytest.raises(RuntimeError):
            asyncio.run(app(scope, receive, send))
        body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        return sent[0]["status"], dict(sent[0]["headers"]), body

    get_status, get_headers, get_body = call("GET")
    head_status, head_headers, head_body = call("HEAD")
    app.state.db.close()
    assert (get_status, head_status) == (500, 500)
    assert json.loads(get_body)["code"] == "internal_error"
    assert head_body == b""
    assert head_headers[b"content-length"] == get_headers[b"content-length"]
    assert head_headers[b"x-request-id"].startswith(b"req_")
