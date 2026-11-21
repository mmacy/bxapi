import asyncio
import json
import sqlite3
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient
from osrlib.crawl.session import GameSession
from starlette.types import Message

from bxapi.app import create_app
from bxapi.db import Database
from bxapi.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    IN_PROGRESS_SECONDS,
    REPLAYED_HEADER,
    RETENTION_SECONDS,
    key_in_progress,
    key_interrupted,
    key_reused,
)
from bxapi.session_manager import SessionManager
from bxapi.settings import Settings

from .conftest import LOCAL_URL, problem_without_id

FIGHTER = {"name": "Brenna", "class_id": "fighter", "alignment": "lawful"}
ENTER = {"command_type": "enter_dungeon", "dungeon_id": "barrow"}


def key(value: str) -> dict[str, str]:
    return {IDEMPOTENCY_KEY_HEADER: value}


def start(client: TestClient) -> str:
    character = client.post("/v1/characters", json=FIGHTER).json()
    session = client.post("/v1/sessions", json={"adventure_id": "builtin-barrow", "character_ids": [character["id"]]})
    return session.json()["id"]


def db_of(client: TestClient) -> Database:
    return client.app.state.db  # pyright: ignore[reportAttributeAccessIssue]


def assert_replay(first: Any, again: Any) -> None:
    assert REPLAYED_HEADER not in first.headers
    assert again.headers[REPLAYED_HEADER] == "true"
    assert (again.status_code, again.content) == (first.status_code, first.content)
    assert again.headers["content-type"] == first.headers["content-type"]
    assert again.headers["x-request-id"] != first.headers["x-request-id"]


# Replays


def test_a_retry_gets_the_first_response_and_creates_nothing(client: TestClient) -> None:
    first = client.post("/v1/characters", json=FIGHTER, headers=key("create-1"))
    assert first.status_code == 201
    for _ in range(3):
        assert_replay(first, client.post("/v1/characters", json=FIGHTER, headers=key("create-1")))
    assert client.get("/v1/characters").json()["total"] == 1
    other = client.post("/v1/characters", json=FIGHTER, headers=key("create-2"))
    assert other.json()["id"] != first.json()["id"]
    assert client.get("/v1/characters").json()["total"] == 2


def test_a_retried_command_runs_once_and_returns_the_first_outcome(client: TestClient) -> None:
    session_id = start(client)
    path = f"/v1/sessions/{session_id}/commands"
    first = client.post(path, json=ENTER, headers=key("enter"))
    assert first.json()["accepted"] and first.json()["revision"] == 1 and first.json()["events"]
    assert_replay(first, client.post(path, json=ENTER, headers=key("enter")))
    assert client.get(f"/v1/sessions/{session_id}").json()["revision"] == 1
    assert client.get(f"/v1/sessions/{session_id}/events").json()["total"] == len(first.json()["events"])


def test_a_refused_command_is_an_outcome_and_replays(client: TestClient) -> None:
    session_id = start(client)
    path = f"/v1/sessions/{session_id}/commands"
    move = {"command_type": "move_party", "direction": "north"}
    refused = client.post(path, json=move, headers=key("move"))
    assert refused.status_code == 200 and not refused.json()["accepted"]
    assert_replay(refused, client.post(path, json=move, headers=key("move")))


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/v1/ability-rolls", None, 201),
        ("/v1/dice/roll", {"expression": "3d6"}, 200),
        ("/v1/generate/treasure", {"treasure_type": "A", "tier": "basic"}, 200),
    ],
)
def test_every_post_replays_including_one_that_rolls_dice(
    client: TestClient, path: str, body: dict[str, Any] | None, status: int
) -> None:
    first = client.post(path, json=body, headers=key("same"))
    assert first.status_code == status
    assert_replay(first, client.post(path, json=body, headers=key("same")))


def test_saving_restoring_and_importing_replay(client: TestClient) -> None:
    session_id = start(client)
    save = client.post(f"/v1/sessions/{session_id}/saves", headers=key("save"))
    assert_replay(save, client.post(f"/v1/sessions/{session_id}/saves", headers=key("save")))
    restore = client.post(f"/v1/saves/{save.json()['id']}/restore", headers=key("restore"))
    assert_replay(restore, client.post(f"/v1/saves/{save.json()['id']}/restore", headers=key("restore")))
    document = client.get(f"/v1/saves/{save.json()['id']}/document").json()
    imported = client.post("/v1/saves/import", json=document, headers=key("import"))
    assert_replay(imported, client.post("/v1/saves/import", json=document, headers=key("import")))
    assert client.get("/v1/saves").json()["total"] == 2
    assert client.get("/v1/sessions").json()["total"] == 2


def test_a_request_without_a_key_runs_every_time(client: TestClient) -> None:
    ids = {client.post("/v1/characters", json=FIGHTER).json()["id"] for _ in range(3)}
    assert len(ids) == 3


def test_get_patch_and_delete_ignore_the_header(client: TestClient) -> None:
    session_id = start(client)
    assert REPLAYED_HEADER not in client.get("/v1/meta", headers=key("k")).headers
    renamed = client.patch(f"/v1/sessions/{session_id}", json={"name": "One"}, headers=key("k"))
    assert renamed.json()["name"] == "One"
    assert client.patch(f"/v1/sessions/{session_id}", json={"name": "Two"}, headers=key("k")).json()["name"] == "Two"
    assert client.delete(f"/v1/sessions/{session_id}", headers=key("k")).status_code == 204
    assert client.delete(f"/v1/sessions/{session_id}", headers=key("k")).status_code == 404


# Whose key it is, and what it covers


def test_a_key_belongs_to_one_client(referee: TestClient, other: TestClient) -> None:
    mine = referee.post("/v1/characters", json=FIGHTER, headers=key("shared-value"))
    theirs = other.post("/v1/characters", json=FIGHTER, headers=key("shared-value"))
    assert REPLAYED_HEADER not in theirs.headers
    assert theirs.json()["id"] != mine.json()["id"]
    assert other.get(f"/v1/characters/{mine.json()['id']}").status_code == 404


def test_a_player_cannot_replay_a_referees_response(player: TestClient, referee: TestClient) -> None:
    session_id = start(referee)
    path = f"/v1/sessions/{session_id}/commands"
    assert referee.post(path, json=ENTER, params={"visibility": "referee"}, headers=key("enter")).status_code == 200
    refused = player.post(path, json=ENTER, params={"visibility": "referee"}, headers=key("enter"))
    assert problem_without_id(refused) == key_reused().problem().model_dump(mode="json")


@pytest.mark.parametrize(
    ("path", "params", "content"),
    [
        ("/v1/characters", None, b'{"name": "Other", "class_id": "fighter", "alignment": "lawful"}'),
        ("/v1/characters", {"x": "1"}, None),
        ("/v1/generate/character", None, None),
        ("/v1/characters", None, json.dumps(FIGHTER).encode() + b" "),
    ],
)
def test_a_key_sent_with_another_request_is_refused(
    client: TestClient, path: str, params: dict[str, str] | None, content: bytes | None
) -> None:
    sent = json.dumps(FIGHTER).encode()
    headers = key("create") | {"Content-Type": "application/json"}
    first = client.post("/v1/characters", content=sent, headers=headers)
    assert first.status_code == 201
    refused = client.post(path, params=params, content=sent if content is None else content, headers=headers)
    assert refused.status_code == 422
    assert problem_without_id(refused) == key_reused().problem().model_dump(mode="json")
    assert_replay(first, client.post("/v1/characters", content=sent, headers=headers))
    assert client.get("/v1/characters").json()["total"] == 1


@pytest.mark.parametrize("value", [b"", b"two words", b"x" * 256, "café".encode(), b"tab\there"])
def test_a_malformed_key_is_a_validation_error(client: TestClient, value: bytes) -> None:
    response = client.post("/v1/characters", json=FIGHTER, headers={IDEMPOTENCY_KEY_HEADER.encode(): value})
    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "validation_error"
    assert problem["errors"][0]["loc"] == ["header", IDEMPOTENCY_KEY_HEADER]
    assert client.get("/v1/characters").json()["total"] == 0


@pytest.mark.parametrize("value", ["a", "x" * 255, "0b6f7c1e-3f9a-4f0e-9f57-2d1c6e8a4b11", "!~{}:/?#[]@"])
def test_a_key_is_any_printable_ascii_without_spaces(client: TestClient, value: str) -> None:
    first = client.post("/v1/ability-rolls", headers=key(value))
    assert_replay(first, client.post("/v1/ability-rolls", headers=key(value)))


# Failures


def test_a_refused_request_frees_its_key(client: TestClient) -> None:
    broke = FIGHTER | {"purchases": [{"item_id": "plate_mail", "quantity": 10}]}
    for _ in range(2):
        refused = client.post("/v1/characters", json=broke, headers=key("create"))
        assert (refused.status_code, refused.json()["code"]) == (422, "character_invalid")
        assert REPLAYED_HEADER not in refused.headers
    missing = {"adventure_id": "adv_nope", "character_ids": ["chr_nope"]}
    assert client.post("/v1/sessions", json=missing, headers=key("start")).status_code == 404
    assert client.post("/v1/dice/roll", json={}, headers=key("roll")).status_code == 422
    assert (
        client.post("/v1/dice/roll", content=b"{}", headers=key("plain") | {"Content-Type": "text/plain"}).status_code
        == 415
    )
    rows = db_of(client)._connection.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0]
    assert rows == 0


def test_a_conflict_frees_the_key_so_the_retry_reports_the_conflict_again(client: TestClient) -> None:
    session_id = start(client)
    path = f"/v1/sessions/{session_id}/commands"
    for _ in range(2):
        conflict = client.post(path, json=ENTER, params={"expected_revision": 5}, headers=key("enter"))
        assert (conflict.status_code, conflict.json()["code"]) == (409, "revision_conflict")


def test_a_retry_after_a_lost_response_and_a_stale_revision_still_replays(client: TestClient) -> None:
    session_id = start(client)
    path = f"/v1/sessions/{session_id}/commands"
    first = client.post(path, json=ENTER, params={"expected_revision": 0}, headers=key("enter"))
    assert first.json()["revision"] == 1
    assert_replay(first, client.post(path, json=ENTER, params={"expected_revision": 0}, headers=key("enter")))


def _buggy(settings: Settings) -> TestClient:
    app = create_app(settings)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("SECRET")

    app.state.db.insert_ability_roll = explode
    return TestClient(app, base_url=LOCAL_URL, raise_server_exceptions=False)


def test_an_unexpected_error_leaves_the_key_interrupted(settings: Settings) -> None:
    with _buggy(settings) as client:
        failed = client.post("/v1/ability-rolls", headers=key("roll"))
        assert (failed.status_code, failed.json()["code"]) == (500, "internal_error")
        for _ in range(2):
            again = client.post("/v1/ability-rolls", headers=key("roll"))
            assert again.status_code == 409
            assert "retry-after" not in again.headers
            assert problem_without_id(again) == key_interrupted().problem().model_dump(mode="json")
        assert client.post("/v1/ability-rolls").status_code == 500


def test_a_response_is_sent_even_when_its_record_cannot_be_written(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    db = db_of(client)
    write = db.complete_idempotency_key
    db.complete_idempotency_key = refuse
    created = client.post("/v1/characters", json=FIGHTER, headers=key("create"))
    db.complete_idempotency_key = write
    assert created.status_code == 201
    assert "idempotency key" in caplog.text
    again = client.post("/v1/characters", json=FIGHTER, headers=key("create"))
    assert (again.status_code, again.json()["code"]) == (409, "idempotency_key_in_progress")
    assert client.get("/v1/characters").json()["total"] == 1


def test_a_second_request_waits_while_the_first_runs(client: TestClient) -> None:
    session_id = start(client)
    path = f"/v1/sessions/{session_id}/commands"
    manager: SessionManager = client.app.state.sessions  # pyright: ignore[reportAttributeAccessIssue]
    entered, release = threading.Event(), threading.Event()

    def hold(_: GameSession) -> None:
        entered.set()
        release.wait(5)

    holder = threading.Thread(target=manager.read, args=("local", session_id, hold))
    holder.start()
    assert entered.wait(5)
    responses: list[Any] = []
    first = threading.Thread(target=lambda: responses.append(client.post(path, json=ENTER, headers=key("enter"))))
    first.start()
    try:
        claimed = "SELECT count(*) FROM idempotency_keys WHERE key = 'enter' AND status IS NULL"
        for _ in range(500):
            if db_of(client)._connection.execute(claimed).fetchone()[0]:
                break
            threading.Event().wait(0.01)
        second = client.post(path, json=ENTER, headers=key("enter"))
        assert second.status_code == 409
        assert second.headers["retry-after"] == "1"
        assert problem_without_id(second) == key_in_progress().problem().model_dump(mode="json")
    finally:
        release.set()
        holder.join()
        first.join()
    assert responses[0].json()["revision"] == 1
    assert_replay(responses[0], client.post(path, json=ENTER, headers=key("enter")))


def test_concurrent_retries_create_one_character(client: TestClient) -> None:
    responses: list[Any] = []
    threads = [
        threading.Thread(
            target=lambda: responses.append(client.post("/v1/characters", json=FIGHTER, headers=key("race")))
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    created = [response for response in responses if response.status_code == 201]
    waiting = [response for response in responses if response.status_code == 409]
    assert len(created) + len(waiting) == 8 and created
    assert len({response.content for response in created}) == 1
    assert {response.json()["code"] for response in waiting} <= {"idempotency_key_in_progress"}
    assert client.get("/v1/characters").json()["total"] == 1


# What the database remembers


def test_a_key_with_no_outcome_is_in_progress_and_then_interrupted(client: TestClient) -> None:
    db = db_of(client)
    assert client.post("/v1/ability-rolls", headers=key("k")).status_code == 201
    db._connection.execute("UPDATE idempotency_keys SET status = NULL, body = NULL")
    assert client.post("/v1/ability-rolls", headers=key("k")).json()["code"] == "idempotency_key_in_progress"
    db._connection.execute("UPDATE idempotency_keys SET claimed_at = claimed_at - ?", (IN_PROGRESS_SECONDS,))
    assert client.post("/v1/ability-rolls", headers=key("k")).json()["code"] == "idempotency_key_interrupted"
    assert client.post("/v1/ability-rolls", json={}, headers=key("k")).json()["code"] == "idempotency_key_reused"


def test_a_claimed_key_returns_its_row() -> None:
    db = Database(":memory:")
    assert db.claim_idempotency_key("a", "k", "f", now=1000, retention_seconds=100) is None
    row = db.claim_idempotency_key("a", "k", "other", now=1001, retention_seconds=100)
    assert row is not None and (row.fingerprint, row.status, row.body, row.claimed_at) == ("f", None, None, 1000)
    assert db.claim_idempotency_key("b", "k", "f", now=1001, retention_seconds=100) is None
    db.close()


def test_the_server_forgets_a_key_after_the_retention_time(client: TestClient) -> None:
    db = db_of(client)
    first = client.post("/v1/ability-rolls", headers=key("k"))
    db._connection.execute("UPDATE idempotency_keys SET claimed_at = claimed_at - ?", (RETENTION_SECONDS - 60,))
    assert_replay(first, client.post("/v1/ability-rolls", headers=key("k")))
    db._connection.execute("UPDATE idempotency_keys SET claimed_at = claimed_at - 60")
    fresh = client.post("/v1/ability-rolls", headers=key("k"))
    assert REPLAYED_HEADER not in fresh.headers
    assert fresh.json()["id"] != first.json()["id"]


def test_claiming_a_key_deletes_every_clients_expired_keys() -> None:
    db = Database(":memory:")
    assert db.claim_idempotency_key("a", "old", "f", now=1000, retention_seconds=100) is None
    assert db.claim_idempotency_key("b", "old", "f", now=1050, retention_seconds=100) is None
    assert db.claim_idempotency_key("c", "new", "f", now=1100, retention_seconds=100) is None
    keys = db._connection.execute("SELECT client, key FROM idempotency_keys ORDER BY client").fetchall()
    assert [tuple(row) for row in keys] == [("b", "old"), ("c", "new")]
    db.close()


def test_releasing_a_key_leaves_a_recorded_outcome() -> None:
    db = Database(":memory:")
    db.claim_idempotency_key("a", "k", "f", now=1000, retention_seconds=100)
    db.complete_idempotency_key("a", "k", status=201, body=b"{}")
    db.release_idempotency_key("a", "k")
    row = db.claim_idempotency_key("a", "k", "f", now=1000, retention_seconds=100)
    assert row is not None and (row.status, row.body) == (201, b"{}")
    db.claim_idempotency_key("a", "j", "f", now=1000, retention_seconds=100)
    db.release_idempotency_key("other", "j")
    assert db.claim_idempotency_key("a", "j", "f", now=1000, retention_seconds=100) is not None
    db.release_idempotency_key("a", "j")
    assert db.claim_idempotency_key("a", "j", "f", now=1000, retention_seconds=100) is None
    db.close()


def test_two_processes_share_the_keys(settings: Settings) -> None:
    with (
        TestClient(create_app(settings), base_url=LOCAL_URL) as one,
        TestClient(create_app(settings), base_url=LOCAL_URL) as two,
    ):
        first = one.post("/v1/characters", json=FIGHTER, headers=key("create"))
        assert_replay(first, two.post("/v1/characters", json=FIGHTER, headers=key("create")))
        assert two.get("/v1/characters").json()["total"] == 1


# The OpenAPI document


def test_the_openapi_document_describes_the_key_on_every_post(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    posts = [item["post"] for path, item in document["paths"].items() if "post" in item]
    assert len(posts) > 10
    for operation in posts:
        assert [p["in"] for p in operation["parameters"] if p["name"] == IDEMPOTENCY_KEY_HEADER] == ["header"]
        assert "idempotency_key_reused" in operation["responses"]["422"]["description"]
        for code in ("idempotency_key_in_progress", "idempotency_key_interrupted"):
            assert code in operation["responses"]["409"]["description"]
            assert code in operation["responses"]["409"]["content"]["application/problem+json"]["examples"]
        created = next(response for status, response in operation["responses"].items() if status.startswith("2"))
        assert created["headers"][REPLAYED_HEADER] == {"$ref": f"#/components/headers/{REPLAYED_HEADER}"}
    for item in document["paths"].values():
        for method in ("get", "patch", "delete"):
            names = [parameter["name"] for parameter in item.get(method, {}).get("parameters", [])]
            assert IDEMPOTENCY_KEY_HEADER not in names


def test_a_client_that_disconnects_in_the_body_gets_400_and_claims_no_key(settings: Settings) -> None:
    app = create_app(settings)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

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
        "headers": [
            (b"host", b"localhost"),
            (b"content-type", b"application/json"),
            (b"content-length", b"100"),
            (b"idempotency-key", b"roll"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8640),
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 400
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert json.loads(body)["code"] == "bad_request"
    assert app.state.db._connection.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0] == 0
    app.state.db.close()
