import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from osrlib.crawl.adventure import PartySpec
from osrlib.versioning import SCHEMA_VERSION

from bxapi.adventures import adventure_document, builtin_adventures, find_adventure, parse_adventure
from bxapi.db import Database
from bxapi.settings import Settings

BARROW_ID = "builtin-barrow"
INFO_KEYS = {"id", "name", "builtin", "created_at", "description", "hooks", "party", "town", "dungeons"}


def barrow() -> Any:
    return builtin_adventures()[BARROW_ID].adventure


def a_document(name: str = "Second Barrow", **changes: Any) -> dict[str, Any]:
    """Return a valid adventure document, the built-in barrow under another name."""
    adventure = barrow().model_copy(update={"name": name, **changes})
    return copy.deepcopy(adventure_document(adventure))


def expected_info(adventure: Any) -> dict[str, Any]:
    return {
        "name": adventure.name,
        "description": adventure.description,
        "hooks": list(adventure.hooks),
        "party": None if adventure.party is None else adventure.party.model_dump(mode="json"),
        "town": {"name": adventure.town.name, "description": adventure.town.description},
        "dungeons": [
            {"id": dungeon.id, "name": dungeon.name, "level_count": len(dungeon.levels)}
            for dungeon in adventure.dungeons
        ],
    }


def problem(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    return body


def test_upload_stores_the_document_as_uploaded(player: TestClient, keyed_settings: Settings) -> None:
    party = PartySpec(min_level=1, max_level=3, min_size=4, max_size=6)
    document = a_document(hooks=("A rumour in the tavern.",), party=party)
    response = player.post("/v1/adventures", json=document)
    assert response.status_code == 201
    info = response.json()
    assert set(info) == INFO_KEYS
    assert info["id"].startswith("adv_") and info["builtin"] is False and info["created_at"]
    assert {key: info[key] for key in expected_info(barrow())} == expected_info(parse_adventure(document))
    assert info["party"] == {"min_level": 1, "max_level": 3, "min_size": 4, "max_size": 6}

    db = Database(keyed_settings.database)
    assert db.get_adventure_document("game", info["id"]) == document
    assert find_adventure(db, "game", info["id"]) == parse_adventure(document)
    db.close()


def test_info_leaves_out_everything_a_player_may_not_know(player: TestClient) -> None:
    info = player.get(f"/v1/adventures/{BARROW_ID}").json()
    assert set(info) == INFO_KEYS
    assert set(info["town"]) == {"name", "description"}
    assert all(set(dungeon) == {"id", "name", "level_count"} for dungeon in info["dungeons"])


def test_upload_errors(player: TestClient) -> None:
    broken = a_document()
    broken["payload"]["dungeons"][0]["levels"][0]["areas"][0]["encounter"] = {
        "monsters": [{"template_id": "no_such_monster", "count_fixed": 1}]
    }
    problem(player.post("/v1/adventures", json=broken), 422, "content_invalid")
    problem(player.post("/v1/adventures", json=a_document() | {"kind": "save"}), 422, "content_invalid")
    newer = a_document() | {"schema_version": SCHEMA_VERSION + 1}
    problem(player.post("/v1/adventures", json=newer), 409, "version_unsupported")
    problem(player.post("/v1/adventures", json=[a_document()]), 422, "validation_error")
    assert player.get("/v1/adventures").json()["total"] == len(builtin_adventures())


def test_validate_reports_without_storing(player: TestClient) -> None:
    good = player.post("/v1/adventures/validate", json=a_document())
    assert good.status_code == 200
    assert good.json() == {
        "valid": True,
        "adventure": expected_info(parse_adventure(a_document())) | {"id": None, "builtin": False, "created_at": None},
    }
    for bad in (a_document() | {"kind": "save"}, a_document() | {"schema_version": SCHEMA_VERSION + 1}):
        result = player.post("/v1/adventures/validate", json=bad)
        assert result.status_code == 200
        assert set(result.json()) == {"valid", "detail"} and result.json()["valid"] is False
    assert player.get("/v1/adventures").json()["total"] == len(builtin_adventures())


def test_the_list_pages_across_built_in_and_stored_adventures(player: TestClient) -> None:
    builtins = len(builtin_adventures())
    stored = [player.post("/v1/adventures", json=a_document(f"Copy {index}")).json() for index in range(3)]
    everything = player.get("/v1/adventures", params={"limit": 200}).json()
    ids = [item["id"] for item in everything["items"]]
    assert ids == [*builtin_adventures(), *(info["id"] for info in reversed(stored))]
    assert everything["total"] == builtins + 3
    for limit in range(1, builtins + 4):
        for offset in range(builtins + 5):
            page = player.get("/v1/adventures", params={"limit": limit, "offset": offset}).json()
            assert [item["id"] for item in page["items"]] == ids[offset : offset + limit]
            assert (page["total"], page["limit"], page["offset"]) == (builtins + 3, limit, offset)


def test_get_and_document_for_a_built_in(player: TestClient, referee: TestClient) -> None:
    info = player.get(f"/v1/adventures/{BARROW_ID}").json()
    assert info == expected_info(barrow()) | {"id": BARROW_ID, "builtin": True, "created_at": None}
    problem(player.get(f"/v1/adventures/{BARROW_ID}/document"), 403, "referee_required")
    document = referee.get(f"/v1/adventures/{BARROW_ID}/document")
    assert document.status_code == 200
    assert document.json() == builtin_adventures()[BARROW_ID].document


def test_the_document_of_a_stored_adventure_is_for_referees(player: TestClient, referee: TestClient) -> None:
    document = a_document()
    info = player.post("/v1/adventures", json=document).json()
    problem(player.get(f"/v1/adventures/{info['id']}/document"), 403, "referee_required")
    assert referee.get(f"/v1/adventures/{info['id']}/document").json() == document


@pytest.mark.parametrize("adventure_id", ["adv_" + "x" * 22, "builtin-missing"])
def test_unknown_ids_are_404(referee: TestClient, adventure_id: str) -> None:
    for response in (
        referee.get(f"/v1/adventures/{adventure_id}"),
        referee.get(f"/v1/adventures/{adventure_id}/document"),
        referee.delete(f"/v1/adventures/{adventure_id}"),
    ):
        body = problem(response, 404, "not_found")
        assert (body["resource"], body["id"]) == ("adventure", adventure_id)


def test_delete(player: TestClient, referee: TestClient) -> None:
    problem(player.delete(f"/v1/adventures/{BARROW_ID}"), 403, "adventure_read_only")
    problem(referee.delete(f"/v1/adventures/{BARROW_ID}"), 403, "adventure_read_only")
    info = player.post("/v1/adventures", json=a_document()).json()
    deleted = player.delete(f"/v1/adventures/{info['id']}")
    assert (deleted.status_code, deleted.content) == (204, b"")
    problem(player.get(f"/v1/adventures/{info['id']}"), 404, "not_found")


def test_another_client_sees_only_the_built_ins(player: TestClient, other: TestClient) -> None:
    info = player.post("/v1/adventures", json=a_document()).json()
    for response in (
        other.get(f"/v1/adventures/{info['id']}"),
        other.get(f"/v1/adventures/{info['id']}/document"),
        other.delete(f"/v1/adventures/{info['id']}"),
    ):
        problem(response, 404, "not_found")
    listed = other.get("/v1/adventures").json()
    assert [item["id"] for item in listed["items"]] == list(builtin_adventures())
    assert listed["total"] == len(builtin_adventures())
    assert player.get(f"/v1/adventures/{info['id']}").json() == info


def test_the_openapi_document_names_the_operations(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    operations = {
        ("/v1/adventures", "post"): "upload_adventure",
        ("/v1/adventures", "get"): "list_adventures",
        ("/v1/adventures/validate", "post"): "validate_adventure",
        ("/v1/adventures/{adventure_id}", "get"): "get_adventure",
        ("/v1/adventures/{adventure_id}", "delete"): "delete_adventure",
        ("/v1/adventures/{adventure_id}/document", "get"): "get_adventure_document",
    }
    for (path, method), operation_id in operations.items():
        operation = paths[path][method]
        assert operation["operationId"] == operation_id
        assert operation["tags"] == ["adventures"]
        assert operation["summary"] and operation["description"]
