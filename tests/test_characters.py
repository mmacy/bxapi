import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from osrlib.core.abilities import AbilityScore
from osrlib.core.alignment import Alignment
from osrlib.core.character import CHARACTER_CREATION_STREAM, Character, create_character, roll_ability_scores
from osrlib.core.classes import level_title, xp_modifier_pct
from osrlib.core.rng import RngStreams
from osrlib.core.ruleset import Ruleset
from osrlib.data import load_classes

from bxapi.creation import eligible_class_ids
from bxapi.db import CharacterDocumentRow, Database
from bxapi.settings import Settings

FIGHTER = {"name": "Hild", "class_id": "fighter", "alignment": "lawful"}
GEAR = {"purchases": [{"item_id": "leather", "quantity": 1}, {"item_id": "dagger"}], "equip_item_ids": ["leather"]}

DERIVED_KEYS = {
    "thac0",
    "attack_bonus",
    "armour_class",
    "armour_class_ascending",
    "saves",
    "melee_modifier",
    "open_doors_chance",
    "missile_modifier",
    "initiative_modifier",
    "hit_point_modifier",
    "magic_save_modifier",
    "npc_reaction_modifier",
    "xp_modifier_pct",
    "level_title",
    "alignment_tongue",
    "languages",
}


def expected_derived(character: Character) -> dict[str, Any]:
    return {
        "thac0": character.thac0,
        "attack_bonus": character.attack_bonus,
        "armour_class": character.armour_class,
        "armour_class_ascending": character.armour_class_ascending,
        "saves": character.saves.model_dump(mode="json"),
        "melee_modifier": character.melee_modifier,
        "open_doors_chance": character.open_doors_chance,
        "missile_modifier": character.missile_modifier,
        "initiative_modifier": character.initiative_modifier,
        "hit_point_modifier": character.hit_point_modifier,
        "magic_save_modifier": character.magic_save_modifier,
        "npc_reaction_modifier": character.npc_reaction_modifier,
        "xp_modifier_pct": xp_modifier_pct(character.definition, character.scores),
        "level_title": level_title(character.definition, character.level),
        "alignment_tongue": character.alignment_tongue,
        "languages": list(character.languages),
    }


def osrlib_character(seed: int, **choices: Any) -> Any:
    return create_character(
        name="Hild",
        class_id="fighter",
        alignment=Alignment.LAWFUL,
        ruleset=Ruleset(),
        stream=RngStreams(master_seed=seed).get(CHARACTER_CREATION_STREAM),
        **choices,
    )


def rolled_scores(seed: int) -> dict[str, int]:
    rolled = roll_ability_scores(RngStreams(master_seed=seed).get(CHARACTER_CREATION_STREAM))
    return {ability.value: score for ability, score in rolled.scores.items()}


def stored_row(settings: Settings, character_id: str) -> CharacterDocumentRow:
    """Read a character's row from the database file the test applications share."""
    db = Database(settings.database)
    row = db.get_character("game", character_id)
    db.close()
    assert row is not None
    return row


def seed_where(wanted: Any) -> int:
    return next(seed for seed in range(500) if wanted(rolled_scores(seed)))


# Ability rolls


def test_a_referee_roll_matches_osrlib_and_lists_the_eligible_classes(referee: TestClient) -> None:
    response = referee.post("/v1/ability-rolls", json={"seed": 7})
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "scores", "eligible_class_ids", "used", "created_at"}
    assert body["id"].startswith("rol_")
    assert body["scores"] == rolled_scores(7)
    scores = {AbilityScore(key): value for key, value in body["scores"].items()}
    assert body["eligible_class_ids"] == eligible_class_ids(scores)
    assert body["used"] is False
    assert "seed" not in body


def test_eligible_classes_come_from_osrlib() -> None:
    scores = dict.fromkeys(AbilityScore, 12)
    scores[AbilityScore.CON] = 7
    every = [definition.id for definition in load_classes().classes]
    assert eligible_class_ids(scores) == [class_id for class_id in every if class_id not in {"dwarf", "halfling"}]


def test_a_player_rolls_without_a_body_but_not_with_a_seed(player: TestClient) -> None:
    assert player.post("/v1/ability-rolls").status_code == 201
    assert player.post("/v1/ability-rolls", json={}).status_code == 201
    refused = player.post("/v1/ability-rolls", json={"seed": 1})
    assert (refused.status_code, refused.json()["code"]) == (403, "referee_required")


def test_get_ability_roll_and_the_client_filter(player: TestClient, other: TestClient) -> None:
    created = player.post("/v1/ability-rolls").json()
    assert player.get(f"/v1/ability-rolls/{created['id']}").json() == created
    missing = other.get(f"/v1/ability-rolls/{created['id']}")
    assert missing.status_code == 404
    assert (missing.json()["code"], missing.json()["resource"]) == ("not_found", "ability roll")


# Creating stored characters


def test_a_character_without_a_roll_matches_osrlib_create_character(referee: TestClient, client: TestClient) -> None:
    response = referee.post("/v1/characters", json=FIGHTER | GEAR | {"seed": 11})
    assert response.status_code == 201
    stored = response.json()
    expected = osrlib_character(11, purchases=[("leather", 1), ("dagger", 1)], equip_ids=["leather"]).character
    assert stored["character"] == expected.model_dump(mode="json")
    assert stored["derived"] == expected_derived(expected)
    assert set(stored["derived"]) == DERIVED_KEYS
    assert (stored["name"], stored["class_id"], stored["level"]) == ("Hild", "fighter", 1)


def test_the_stored_document_is_the_osrlib_document(referee: TestClient, keyed_settings: Settings) -> None:
    stored = referee.post("/v1/characters", json=FIGHTER | GEAR | {"seed": 3}).json()
    row = stored_row(keyed_settings, stored["id"])
    character = Character.from_document(row.document)
    assert row.document == character.to_document()
    assert character.model_dump(mode="json") == stored["character"]
    assert row.max_hp == character.max_hp


def test_a_character_from_a_roll_uses_its_scores_and_draws_hp_and_gold(referee: TestClient) -> None:
    roll = referee.post("/v1/ability-rolls", json={"seed": 21}).json()
    body = FIGHTER | {"ability_roll_id": roll["id"], "seed": 5}
    stored = referee.post("/v1/characters", json=body).json()
    assert stored["character"]["scores"] == roll["scores"]
    # With no ability roll to make, the hit points and the gold are the first draws of the stream.
    stream = RngStreams(master_seed=5).get(CHARACTER_CREATION_STREAM)
    fighter = load_classes().get("fighter")
    character = Character.model_validate(stored["character"])
    first_die = stream.randbelow(fighter.row(1).hit_dice.die) + 1
    assert character.max_hp == max(1, first_die + character.hit_point_modifier)
    assert referee.get(f"/v1/ability-rolls/{roll['id']}").json()["used"] is True


def test_rejections_are_osrlibs(referee: TestClient) -> None:
    roll = referee.post("/v1/ability-rolls", json={"seed": seed_where(lambda scores: scores["con"] < 9)}).json()
    body = {"name": "Durin", "class_id": "dwarf", "alignment": "lawful", "ability_roll_id": roll["id"]}
    problem = referee.post("/v1/characters", json=body).json()
    assert problem["code"] == "character_invalid"
    assert problem["rejections"][0] == {
        "code": "creation.class.requirements_not_met",
        "params": {"class": "dwarf", "ability": "con", "minimum": 9, "score": roll["scores"]["con"]},
    }
    assert referee.get(f"/v1/ability-rolls/{roll['id']}").json()["used"] is False


@pytest.mark.parametrize(
    ("choices", "codes"),
    [
        ({"class_id": "magic_user"}, ["magic.book.capacity_mismatch"]),
        ({"starting_spell_ids": ["sleep"]}, ["magic.book.not_arcane"]),
        ({"adjustment": {"lowered": {"dex": 2}, "raised": {"str": 1}}}, ["creation.adjustment.not_lowerable"]),
        ({"extra_languages": ["no_such_language"]}, None),
        ({"purchases": [{"item_id": "plate_mail", "quantity": 4}]}, ["items.purchase.insufficient_funds"]),
        ({"class_id": "cleric", "purchases": [{"item_id": "sword"}], "equip_item_ids": ["sword"]}, None),
    ],
)
def test_each_refused_step_is_character_invalid(
    player: TestClient, choices: dict[str, Any], codes: list[str] | None
) -> None:
    response = player.post("/v1/characters", json=FIGHTER | choices)
    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "character_invalid"
    assert problem["rejections"]
    if codes is not None:
        assert set(codes) <= {rejection["code"] for rejection in problem["rejections"]}
    assert player.get("/v1/characters").json()["total"] == 0


@pytest.mark.parametrize(
    "choices",
    [{"class_id": "no_such_class"}, {"purchases": [{"item_id": "no_such_item"}]}, {"equip_item_ids": ["sword"]}],
)
def test_an_unknown_id_or_an_unbought_item_is_character_invalid_without_rejections(
    player: TestClient, choices: dict[str, Any]
) -> None:
    problem = player.post("/v1/characters", json=FIGHTER | choices).json()
    assert problem["code"] == "character_invalid"
    assert "rejections" not in problem


@pytest.mark.parametrize(
    "body",
    [
        {"class_id": "fighter", "alignment": "lawful"},
        FIGHTER | {"name": ""},
        FIGHTER | {"alignment": "evil"},
        FIGHTER | {"purchases": [{"item_id": "dagger", "quantity": 0}]},
        FIGHTER | {"purchases": [{"item_id": "dagger", "lots": 1}]},
        FIGHTER | {"adjustment": {"lowered": {"int": -2}}},
        FIGHTER | {"seed": -1},
    ],
)
def test_a_malformed_body_is_a_validation_error(player: TestClient, body: dict[str, Any]) -> None:
    response = player.post("/v1/characters", json=body)
    assert (response.status_code, response.json()["code"]) == (422, "validation_error")


def test_a_player_cannot_seed_a_character(player: TestClient) -> None:
    response = player.post("/v1/characters", json=FIGHTER | {"seed": 1})
    assert (response.status_code, response.json()["code"]) == (403, "referee_required")


def test_a_used_roll_is_a_conflict_and_an_unknown_roll_is_not_found(player: TestClient, other: TestClient) -> None:
    roll = player.post("/v1/ability-rolls").json()
    assert player.post("/v1/characters", json=FIGHTER | {"ability_roll_id": roll["id"]}).status_code == 201
    again = player.post("/v1/characters", json=FIGHTER | {"ability_roll_id": roll["id"]})
    assert (again.status_code, again.json()["code"]) == (409, "ability_roll_used")
    fresh = player.post("/v1/ability-rolls").json()
    stolen = other.post("/v1/characters", json=FIGHTER | {"ability_roll_id": fresh["id"]})
    assert (stolen.status_code, stolen.json()["id"]) == (404, fresh["id"])
    assert player.get(f"/v1/ability-rolls/{fresh['id']}").json()["used"] is False


def race_for_a_roll(player: TestClient, referee: TestClient, workers: int) -> list[int]:
    """Send one creation from each of `workers` threads, all from one new roll, and return the statuses."""
    roll = player.post("/v1/ability-rolls").json()
    body = FIGHTER | {"ability_roll_id": roll["id"]}
    barrier = threading.Barrier(workers)

    def create(index: int) -> int:
        barrier.wait()
        return (player if index % 2 else referee).post("/v1/characters", json=body).status_code

    with ThreadPoolExecutor(workers) as pool:
        return sorted(pool.map(create, range(workers)))


def test_racing_creations_from_two_servers_make_one_character(player: TestClient, referee: TestClient) -> None:
    # The player and referee fixtures are two applications, each with a connection to one database file.
    for _ in range(5):
        assert race_for_a_roll(player, referee, 8) == [201] + [409] * 7
    assert player.get("/v1/characters").json()["total"] == 5


# Import, list, get, and delete


def test_import_stores_the_document_and_clears_the_session_id(referee: TestClient, keyed_settings: Settings) -> None:
    character = osrlib_character(4).character
    character.id = "character-0001"
    response = referee.post("/v1/characters/import", json=character.to_document())
    assert response.status_code == 201
    stored = response.json()
    assert stored["character"]["id"] is None
    assert stored["derived"] == expected_derived(character)
    row = stored_row(keyed_settings, stored["id"])
    assert Character.from_document(row.document).model_dump(mode="json") == stored["character"]


def test_import_errors(referee: TestClient, player: TestClient) -> None:
    document = osrlib_character(4).character.to_document()
    refused = player.post("/v1/characters/import", json=document)
    assert (refused.status_code, refused.json()["code"]) == (403, "referee_required")
    newer = referee.post("/v1/characters/import", json=document | {"schema_version": 10**6})
    assert (newer.status_code, newer.json()["code"]) == (409, "version_unsupported")
    party = referee.post("/v1/characters/import", json=document | {"kind": "party"})
    assert (party.status_code, party.json()["code"]) == (422, "content_invalid")
    assert referee.post("/v1/characters/import", json=[document]).json()["code"] == "validation_error"
    assert referee.get("/v1/characters").json()["total"] == 0


def test_list_get_delete_and_the_client_filter(player: TestClient, other: TestClient) -> None:
    made = [player.post("/v1/characters", json=FIGHTER | {"name": name}).json() for name in ("A", "B", "C")]
    page = player.get("/v1/characters", params={"limit": 2}).json()
    assert [item["id"] for item in page["items"]] == [made[2]["id"], made[1]["id"]]
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 0)
    assert page["items"][0] == {key: made[2][key] for key in ("id", "name", "class_id", "level", "created_at")} | {
        "max_hp": made[2]["character"]["max_hp"]
    }
    assert player.get(f"/v1/characters/{made[0]['id']}").json() == made[0]

    assert other.get("/v1/characters").json()["total"] == 0
    assert other.get(f"/v1/characters/{made[0]['id']}").status_code == 404
    assert other.delete(f"/v1/characters/{made[0]['id']}").status_code == 404

    deleted = player.delete(f"/v1/characters/{made[0]['id']}")
    assert (deleted.status_code, deleted.content) == (204, b"")
    gone = player.get(f"/v1/characters/{made[0]['id']}")
    assert (gone.status_code, gone.json()["resource"]) == (404, "character")
    assert player.get("/v1/characters").json()["total"] == 2


# The generator


def test_generate_matches_osrlib_and_stores_nothing(player: TestClient) -> None:
    response = player.post("/v1/generate/character", json=FIGHTER | GEAR | {"seed": 9})
    assert response.status_code == 200
    body = response.json()
    result = osrlib_character(9, purchases=[("leather", 1), ("dagger", 1)], equip_ids=["leather"])
    assert body == {
        "seed": 9,
        "character": result.character.model_dump(mode="json"),
        "derived": expected_derived(result.character),
        "rolls": {
            "ability_rolls": result.ability_rolls.model_dump(mode="json"),
            "hit_point_roll": result.hit_point_roll.model_dump(mode="json"),
            "gold_roll": result.gold_roll.model_dump(mode="json"),
        },
    }
    assert player.get("/v1/characters").json()["total"] == 0


def test_generate_draws_and_returns_a_seed(player: TestClient) -> None:
    first = player.post("/v1/generate/character", json=FIGHTER).json()
    again = player.post("/v1/generate/character", json=FIGHTER | {"seed": first["seed"]}).json()
    assert again == first


def test_generate_refuses_a_roll_id_and_a_broken_rule(player: TestClient) -> None:
    with_roll = player.post("/v1/generate/character", json=FIGHTER | {"ability_roll_id": "rol_x"})
    assert with_roll.json()["code"] == "validation_error"
    refused = player.post("/v1/generate/character", json=FIGHTER | {"starting_spell_ids": ["sleep"]})
    assert (refused.status_code, refused.json()["code"]) == (422, "character_invalid")


def test_the_openapi_document_names_and_tags_the_operations(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    operations = {
        ("/v1/ability-rolls", "post"): "create_ability_roll",
        ("/v1/ability-rolls/{roll_id}", "get"): "get_ability_roll",
        ("/v1/characters", "post"): "create_character",
        ("/v1/characters", "get"): "list_characters",
        ("/v1/characters/import", "post"): "import_character",
        ("/v1/characters/{character_id}", "get"): "get_character",
        ("/v1/characters/{character_id}", "delete"): "delete_character",
        ("/v1/generate/character", "post"): "generate_character",
    }
    for (path, method), operation_id in operations.items():
        operation = paths[path][method]
        assert operation["operationId"] == operation_id
        assert operation["tags"] == (["generate"] if "generate" in path else ["characters"])
        assert operation["summary"] and operation["description"]
