import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bxapi.db import LAYOUT_VERSION, Database, StorageFull
from bxapi.errors import PROBLEM_MEDIA_TYPE, storage_full
from bxapi.openapi import build_openapi
from bxapi.settings import Settings

from .conftest import OTHER_KEY, PLAYER_KEY, REFEREE_KEY, problem_without_id
from .test_adventures import a_document
from .test_characters import FIGHTER
from .test_sessions import start

ME = "game"
ENVELOPE = {"schema_version": 4, "engine_version": "2.5.0"}


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")).encode())


# The count


def test_the_count_follows_every_insert_update_and_delete() -> None:
    db = Database(":memory:")
    assert db.get_usage(ME) == 0
    scores = {"str": 12, "int": 9}
    db.insert_ability_roll(ME, scores=scores, seed=7)
    expected = _size(scores)
    assert db.get_usage(ME) == expected

    sheet = {"name": "Hild", "notes": "x" * 500}
    character = db.insert_character(ME, name="Hild", class_id="fighter", level=1, max_hp=8, document=sheet)
    expected += _size(sheet)
    assert db.get_usage(ME) == expected

    info, document = {"name": "Barrow"}, {"payload": "y" * 300}
    adventure = db.insert_adventure(ME, name="Barrow", info=info, document=document)
    expected += _size(info) + _size(document)
    assert db.get_usage(ME) == expected

    first, second = ENVELOPE | {"log": []}, ENVELOPE | {"log": ["moved"] * 40}
    session = db.insert_session(
        ME, name="Game", adventure_id=None, adventure_name="Barrow", mode="town", clock_rounds=0, document=first
    )
    expected += _size(first)
    assert db.get_usage(ME) == expected
    assert db.update_session(
        ME, session.id, expected_revision=0, document=second, mode="town", clock_rounds=1, revision=1, updated_at="t"
    )
    expected += _size(second) - _size(first)
    assert db.get_usage(ME) == expected
    assert db.rename_session(ME, session.id, "Another name")
    assert db.get_usage(ME) == expected

    save = db.insert_save(
        ME,
        session_id=session.id,
        adventure_id=None,
        adventure_name="Barrow",
        label=None,
        mode="town",
        clock_rounds=1,
        document=second,
    )
    expected += _size(second)
    assert db.get_usage(ME) == expected

    for delete, row_id, freed in (
        (db.delete_save, save.id, _size(second)),
        (db.delete_session, session.id, _size(second)),
        (db.delete_adventure, adventure.id, _size(info) + _size(document)),
        (db.delete_character, character.id, _size(sheet)),
    ):
        assert delete(ME, row_id)
        expected -= freed
        assert db.get_usage(ME) == expected
    assert expected == _size(scores)
    assert db.get_usage("other") == 0
    db.close()


def test_the_count_is_bytes_and_each_client_has_its_own() -> None:
    db = Database(":memory:")
    db.insert_character(ME, name="Æ", class_id="fighter", level=1, max_hp=8, document={"x": "\N{GRINNING FACE}"})
    db.insert_character("other", name="B", class_id="fighter", level=1, max_hp=8, document={})
    # The database writes JSON with every character outside ASCII as an escape.
    assert db.get_usage(ME) == len(b'{"x":"\\ud83d\\ude00"}')
    assert db.get_usage("other") == 2
    db.close()


def test_the_count_includes_a_stored_response_until_its_key_expires() -> None:
    db = Database(":memory:")
    assert db.claim_idempotency_key(ME, "roll", "f", now=1000, retention_seconds=100) is None
    assert db.get_usage(ME) == 0
    db.complete_idempotency_key(ME, "roll", status=200, body=b"x" * 64)
    assert db.get_usage(ME) == 64
    assert db.claim_idempotency_key(ME, "claimed-and-released", "f", now=1001, retention_seconds=100) is None
    db.release_idempotency_key(ME, "claimed-and-released")
    assert db.get_usage(ME) == 64
    assert db.claim_idempotency_key("other", "later", "f", now=1100, retention_seconds=100) is None
    assert db.get_usage(ME) == 0
    db.close()


def test_a_database_file_keeps_the_count_and_has_the_new_layout(tmp_path: Path) -> None:
    path = str(tmp_path / "bxapi.db")
    db = Database(path)
    db.insert_ability_roll(ME, scores={"str": 12}, seed=7)
    db.close()
    again = Database(path)
    assert again.get_usage(ME) == len(b'{"str":12}')
    again.close()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == LAYOUT_VERSION == 3
    triggers = connection.execute("SELECT count(*) FROM sqlite_schema WHERE type = 'trigger'").fetchone()[0]
    connection.close()
    assert triggers == 18


# The limit


def test_a_database_with_a_limit_refuses_each_write_at_the_limit() -> None:
    db = Database(":memory:", max_client_bytes=100)
    sheet = {"notes": "x" * 200}
    character = db.insert_character(ME, name="Hild", class_id="fighter", level=1, max_hp=8, document=sheet)
    used = db.get_usage(ME)
    assert used == _size(sheet) > 100
    session_fields: dict[str, Any] = {"adventure_id": None, "adventure_name": "B", "mode": "town", "clock_rounds": 0}
    writes: list[Callable[[], object]] = [
        lambda: db.require_room(ME),
        lambda: db.insert_ability_roll(ME, scores={}, seed=1),
        lambda: db.insert_character(ME, name="B", class_id="fighter", level=1, max_hp=8, document={}),
        lambda: db.insert_adventure(ME, name="B", info={}, document={}),
        lambda: db.insert_session(ME, name="G", document=dict(ENVELOPE), **session_fields),
        lambda: db.insert_save(ME, session_id=None, label=None, document={}, **session_fields),
        lambda: db.claim_idempotency_key(ME, "new", "f", now=1000, retention_seconds=100),
    ]
    for write in writes:
        with pytest.raises(StorageFull) as refused:
            write()
        assert (refused.value.used_bytes, refused.value.max_bytes) == (used, 100)
    assert db.get_usage(ME) == used
    assert db.list_characters(ME, limit=10, offset=0).total == 1
    db.insert_character("other", name="B", class_id="fighter", level=1, max_hp=8, document={})
    assert db.delete_character(ME, character.id)
    for write in writes:
        write()
    db.close()


def test_a_full_client_cannot_use_an_ability_roll_or_write_a_session() -> None:
    db = Database(":memory:", max_client_bytes=150)
    roll = db.insert_ability_roll(ME, scores={"str": 12}, seed=7)
    session = db.insert_session(
        ME, name="G", adventure_id=None, adventure_name="B", mode="town", clock_rounds=0, document=ENVELOPE | {"x": ""}
    )
    filler = db.insert_character(ME, name="F", class_id="fighter", level=1, max_hp=8, document={"x": "y" * 200})
    fields: dict[str, Any] = {"name": "Hild", "class_id": "fighter", "level": 1, "max_hp": 8, "document": {}}
    update: dict[str, Any] = {"mode": "town", "clock_rounds": 1, "revision": 1, "updated_at": "t"}
    with pytest.raises(StorageFull):
        db.insert_character_from_ability_roll(ME, roll.id, **fields)
    with pytest.raises(StorageFull):
        db.update_session(ME, session.id, expected_revision=0, document=dict(ENVELOPE), **update)
    unused = db.get_ability_roll(ME, roll.id)
    assert unused is not None and not unused.used
    assert db.get_session_revision(ME, session.id) == 0
    assert db.delete_character(ME, filler.id)
    assert db.insert_character_from_ability_roll(ME, roll.id, **fields) is not None
    assert db.update_session(ME, session.id, expected_revision=0, document=dict(ENVELOPE), **update)
    db.close()


def test_a_refused_claim_still_forgets_the_expired_keys() -> None:
    db = Database(":memory:", max_client_bytes=50)
    assert db.claim_idempotency_key(ME, "old", "f", now=1000, retention_seconds=100) is None
    db.complete_idempotency_key(ME, "old", status=200, body=b"x" * 80)
    earlier = db.claim_idempotency_key(ME, "old", "f", now=1050, retention_seconds=100)
    assert earlier is not None and earlier.status == 200
    with pytest.raises(StorageFull):
        db.claim_idempotency_key(ME, "new", "f", now=1050, retention_seconds=100)
    assert db.claim_idempotency_key(ME, "new", "f", now=1100, retention_seconds=100) is None
    assert db.get_usage(ME) == 0
    db.close()


# The server


@pytest.fixture
def small(keyed_settings: Settings) -> Settings:
    """Settings with three API keys and room for 20,000 bytes of documents for each client."""
    return keyed_settings.model_copy(update={"max_client_bytes": 20_000})


def _usage(client: TestClient) -> dict[str, Any]:
    return client.get("/v1/meta").json()["storage"]


def _fill(client: TestClient) -> dict[str, Any]:
    """Create characters until the server refuses one, and return the refusal's problem document."""
    for _ in range(100):
        response = client.post("/v1/characters", json=FIGHTER)
        if response.status_code != 201:
            assert response.status_code == 507, response.text
            assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
            return problem_without_id(response)
    raise AssertionError("the server never refused a character")


def test_meta_reports_the_count_and_the_limit(small: Settings, make_client: Callable[..., TestClient]) -> None:
    player = make_client(small, PLAYER_KEY)
    assert _usage(player) == {"used_bytes": 0, "max_bytes": 20_000}
    stored = player.post("/v1/characters", json=FIGHTER).json()
    used = _usage(player)["used_bytes"]
    assert 0 < used < 20_000
    assert _usage(make_client(small, REFEREE_KEY))["used_bytes"] == used
    assert _usage(make_client(small, OTHER_KEY))["used_bytes"] == 0
    assert player.delete(f"/v1/characters/{stored['id']}").status_code == 204
    assert _usage(player)["used_bytes"] == 0
    unlimited = make_client(small.model_copy(update={"max_client_bytes": 0, "database": ":memory:"}), PLAYER_KEY)
    assert _usage(unlimited) == {"used_bytes": 0, "max_bytes": None}


def test_a_client_at_the_limit_gets_507_until_it_deletes_something(
    small: Settings, make_client: Callable[..., TestClient]
) -> None:
    player = make_client(small, PLAYER_KEY)
    problem = _fill(player)
    used = _usage(player)["used_bytes"]
    assert used >= 20_000
    assert problem == storage_full(used, 20_000).problem().model_dump(mode="json")
    total = player.get("/v1/characters").json()["total"]

    assert player.post("/v1/ability-rolls").status_code == 507
    assert player.post("/v1/adventures", json=a_document()).status_code == 507
    assert player.post("/v1/adventures/validate", json=a_document()).status_code == 200
    assert player.get("/v1/characters").json()["total"] == total
    assert make_client(small, OTHER_KEY).post("/v1/characters", json=FIGHTER).status_code == 201

    newest = player.get("/v1/characters").json()["items"][0]["id"]
    assert player.delete(f"/v1/characters/{newest}").status_code == 204
    assert _usage(player)["used_bytes"] < 20_000
    assert player.post("/v1/characters", json=FIGHTER).status_code == 201


def test_a_full_client_can_still_read_rename_and_roll_without_a_key(
    small: Settings, make_client: Callable[..., TestClient]
) -> None:
    referee = make_client(small.model_copy(update={"max_client_bytes": 60_000}), REFEREE_KEY)
    session = start(referee)
    save = referee.post(f"/v1/sessions/{session['id']}/saves").json()
    _fill(referee)
    revision = referee.get(f"/v1/sessions/{session['id']}").json()["revision"]

    command = {"command_type": "enter_dungeon", "dungeon_id": "barrow"}
    refused = referee.post(f"/v1/sessions/{session['id']}/commands", json=command)
    assert (refused.status_code, refused.json()["code"]) == (507, "storage_full")
    assert referee.get(f"/v1/sessions/{session['id']}").json()["revision"] == revision
    assert referee.post(f"/v1/sessions/{session['id']}/saves").status_code == 507
    assert referee.post(f"/v1/saves/{save['id']}/restore").status_code == 507

    assert referee.get(f"/v1/sessions/{session['id']}/view").status_code == 200
    assert referee.patch(f"/v1/sessions/{session['id']}", json={"name": "Renamed"}).status_code == 200
    assert referee.patch(f"/v1/saves/{save['id']}", json={"label": "Kept"}).status_code == 200
    assert referee.post("/v1/dice/roll", json={"expression": "1d6"}).status_code == 200
    with_key = referee.post("/v1/dice/roll", json={"expression": "1d6"}, headers={"Idempotency-Key": "roll-1"})
    assert (with_key.status_code, with_key.json()["code"]) == (507, "storage_full")

    assert referee.delete(f"/v1/saves/{save['id']}").status_code == 204
    accepted = referee.post(f"/v1/sessions/{session['id']}/commands", json=command)
    assert (accepted.status_code, accepted.json()["revision"]) == (200, revision + 1)
    assert referee.post("/v1/dice/roll", json={"expression": "1d6"}, headers={"Idempotency-Key": "roll-1"}).is_success


def test_the_count_is_what_the_tables_store_after_play(small: Settings, make_client: Callable[..., TestClient]) -> None:
    referee = make_client(small.model_copy(update={"max_client_bytes": 10**9}), REFEREE_KEY)
    session = start(referee)
    referee.post(
        f"/v1/sessions/{session['id']}/commands", json={"command_type": "enter_dungeon", "dungeon_id": "barrow"}
    )
    referee.post(f"/v1/sessions/{session['id']}/saves", json={"label": "Before the door"})
    referee.post("/v1/characters", json=FIGHTER, headers={"Idempotency-Key": "create"})
    referee.post("/v1/ability-rolls")
    connection = sqlite3.connect(small.database)
    stored = sum(
        connection.execute(
            f"SELECT coalesce(sum(length(CAST({column} AS BLOB))), 0) FROM {table} WHERE client = ?", (ME,)
        ).fetchone()[0]
        for table, column in [
            ("ability_rolls", "scores"),
            ("characters", "document"),
            ("sessions", "document"),
            ("saves", "document"),
            ("idempotency_keys", "body"),
        ]
    )
    connection.close()
    assert _usage(referee)["used_bytes"] == stored > 10_000


def test_the_openapi_document_lists_507_on_every_post_under_v1() -> None:
    document = build_openapi()
    for path, item in document["paths"].items():
        for method, operation in item.items():
            listed = "507" in operation["responses"]
            assert listed == (method == "post" and path.startswith("/v1/")), (method, path)
    problem = document["components"]["schemas"]["Problem"]["properties"]
    assert {"used_bytes", "max_bytes"} <= set(problem)
    assert "BXAPI_MAX_CLIENT_BYTES" in document["info"]["description"]
