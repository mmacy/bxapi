"""Acceptance tests for ability rolls and stored characters ("Characters" in docs/spec.md)."""

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from osrlib.core.abilities import AbilityAdjustment, AbilityScore, apply_adjustment, validate_adjustment
from osrlib.core.character import Character, validate_class_choice
from osrlib.data import load_ability_tables, load_classes
from starlette.testclient import TestClient

from bxapi.seeds import MAX_SEED

from .conftest import (
    CHARACTER_KEYS,
    CHARACTER_SUMMARY_KEYS,
    assert_id,
    assert_page,
    assert_timestamp,
    build_party,
    create_character_via_api,
    expect,
    expect_not_found,
    future_document,
    json_copy,
    leaves,
)

ROLL_KEYS = {"id", "scores", "eligible_class_ids", "created_at"}


def scores_of(body: Mapping[str, int]) -> dict[AbilityScore, int]:
    """Return a JSON score table as osrlib's score table."""
    return {AbilityScore(key): value for key, value in body.items()}


def eligible_classes(scores: Mapping[str, int]) -> set[str]:
    """Return the classes osrlib says a set of rolled scores qualifies for."""
    table = scores_of(scores)
    return {definition.id for definition in load_classes().classes if not validate_class_choice(table, definition)}


def roll(client: TestClient, **body: Any) -> dict[str, Any]:
    """Ask for an ability roll and return it."""
    return expect(client.post("/v1/ability-rolls", json=body), 201)


def find_roll(referee: TestClient, wanted: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    """Return the first roll, over a fixed list of referee seeds, that `wanted` accepts."""
    for seed in range(1, 80):
        candidate = roll(referee, seed=seed)
        if wanted(candidate):
            return candidate
    raise AssertionError("no roll from seeds 1 to 79 met the test's condition")


def check_stored(stored: Any, *, name: str, class_id: str) -> Character:
    """Check a stored character's fields, and return its osrlib character."""
    assert set(stored) == CHARACTER_KEYS, f"a stored character has the keys {sorted(stored)}"
    assert_id(stored["id"], "chr")
    assert_timestamp(stored["created_at"])
    character = Character.model_validate(stored["character"])
    assert (stored["name"], stored["class_id"], stored["level"]) == (name, class_id, 1)
    assert (character.name, character.class_id, character.level) == (name, class_id, 1)
    return character


def check_derived(derived: Any, character: Character) -> None:
    """Check that `derived` contains each value the spec lists, equal to osrlib's property.

    The spec doesn't name the fields of `derived`, so the check looks for the values: THAC0
    under a key that names it, the saving throws as osrlib's `SavingThrows` model, the languages
    as a list, and both armour classes and the ability modifiers as numbers.
    """
    # SPEC QUESTION: the spec lists what `derived` contains but not the field names. Should they
    # be osrlib's property names (`thac0`, `armour_class`, `armour_class_ascending`, `saves`,
    # `melee_modifier`, ..., `languages`)? Until it says, this check matches values only.
    found = list(leaves(derived))
    assert any(path and "thac0" in path[-1].lower() and value == character.thac0 for path, value in found), (
        f"derived has no THAC0 of {character.thac0}: {derived}"
    )
    assert any(value == character.saves.model_dump(mode="json") for _, value in found), (
        f"derived has no saving throws equal to {character.saves.model_dump(mode='json')}: {derived}"
    )
    assert any(value == list(character.languages) for _, value in found), (
        f"derived has no language list equal to {list(character.languages)}: {derived}"
    )
    numbers = [value for _, value in found if isinstance(value, int) and not isinstance(value, bool)]
    expected = {
        "armour_class": character.armour_class,
        "armour_class_ascending": character.armour_class_ascending,
        "melee_modifier": character.melee_modifier,
        "missile_modifier": character.missile_modifier,
        "initiative_modifier": character.initiative_modifier,
        "hit_point_modifier": character.hit_point_modifier,
        "magic_save_modifier": character.magic_save_modifier,
        "npc_reaction_modifier": character.npc_reaction_modifier,
    }
    for name, value in expected.items():
        assert value in numbers, f"derived has no number equal to the character's {name}, {value}: {derived}"


def test_an_ability_roll_lists_the_classes_osrlib_allows(player: TestClient) -> None:
    for _ in range(6):
        created = roll(player)
        assert ROLL_KEYS <= set(created), f"an ability roll has the keys {sorted(created)}"
        assert_id(created["id"], "rol")
        assert_timestamp(created["created_at"])
        assert set(created["scores"]) == {ability.value for ability in AbilityScore}
        assert all(3 <= score <= 18 for score in created["scores"].values())
        assert sorted(created["eligible_class_ids"]) == sorted(eligible_classes(created["scores"]))
        fetched = expect(player.get(f"/v1/ability-rolls/{created['id']}"), 200)
        assert fetched["used"] is False
        assert {key: fetched[key] for key in ROLL_KEYS} == {key: created[key] for key in ROLL_KEYS}


def test_a_player_cannot_choose_the_seed_of_a_roll(player: TestClient) -> None:
    expect(player.post("/v1/ability-rolls", json={"seed": 5}), 403, "referee_required")


def test_a_referee_seed_repeats_a_roll(referee: TestClient) -> None:
    first = roll(referee, seed=99)
    second = roll(referee, seed=99)
    assert first["id"] != second["id"]
    assert first["scores"] == second["scores"]
    assert first["eligible_class_ids"] == second["eligible_class_ids"]


def test_a_roll_seed_goes_up_to_the_largest_safe_integer(referee: TestClient) -> None:
    assert roll(referee, seed=MAX_SEED)["scores"] == roll(referee, seed=MAX_SEED)["scores"]
    expect(referee.post("/v1/ability-rolls", json={"seed": MAX_SEED + 1}), 422, "validation_error")
    expect(referee.post("/v1/ability-rolls", json={"seed": -1}), 422, "validation_error")
    body = {"name": "Edge", "class_id": "fighter", "alignment": "lawful", "seed": MAX_SEED + 1}
    expect(referee.post("/v1/characters", json=body), 422, "validation_error")
    create_character_via_api(referee, seed=MAX_SEED)


def test_an_unknown_field_is_422(player: TestClient) -> None:
    body = {"name": "Typo", "class_id": "fighter", "alignment": "lawful", "alignmnet": "chaotic"}
    expect(player.post("/v1/characters", json=body), 422, "validation_error")
    expect(player.post("/v1/ability-rolls", json={"sead": 1}), 422, "validation_error")
    assert expect(player.get("/v1/characters"), 200)["total"] == 0


def test_a_character_from_a_roll_has_its_scores_and_uses_it(player: TestClient) -> None:
    created = roll(player)
    stored = create_character_via_api(player, name="Rolled", ability_roll_id=created["id"])
    character = check_stored(stored, name="Rolled", class_id="fighter")
    assert {ability.value: score for ability, score in character.scores.items()} == created["scores"]
    assert expect(player.get(f"/v1/ability-rolls/{created['id']}"), 200)["used"] is True


def test_a_roll_starts_only_one_character(player: TestClient) -> None:
    created = roll(player)
    create_character_via_api(player, ability_roll_id=created["id"])
    again = player.post(
        "/v1/characters",
        json={"name": "Twice", "class_id": "cleric", "alignment": "neutral", "ability_roll_id": created["id"]},
    )
    expect(again, 409, "ability_roll_used")
    assert expect(player.get("/v1/characters"), 200)["total"] == 1


def test_creations_racing_for_one_roll_make_one_character(player: TestClient) -> None:
    created = roll(player)
    body = {"name": "Racer", "class_id": "fighter", "alignment": "lawful", "ability_roll_id": created["id"]}
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: player.post("/v1/characters", json=body), range(6)))
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409, 409, 409, 409, 409], f"the racing creations returned {statuses}"
    assert all(r.json()["code"] == "ability_roll_used" for r in responses if r.status_code == 409)
    assert expect(player.get("/v1/characters"), 200)["total"] == 1


def test_a_failed_creation_leaves_the_roll_unused(player: TestClient) -> None:
    created = roll(player)
    # A magic-user must start with one first-level spell, so this creation breaks a rule.
    refused = player.post(
        "/v1/characters",
        json={"name": "Spell-less", "class_id": "magic_user", "alignment": "neutral", "ability_roll_id": created["id"]},
    )
    expect(refused, 422, "character_invalid")
    assert expect(player.get(f"/v1/ability-rolls/{created['id']}"), 200)["used"] is False
    assert expect(player.get("/v1/characters"), 200)["total"] == 0
    create_character_via_api(player, ability_roll_id=created["id"])
    assert expect(player.get(f"/v1/ability-rolls/{created['id']}"), 200)["used"] is True


def test_a_class_the_rolled_scores_miss_is_refused(referee: TestClient) -> None:
    every_class = {definition.id for definition in load_classes().classes}
    created = find_roll(referee, lambda candidate: bool(every_class - set(candidate["eligible_class_ids"])))
    closed = sorted(every_class - set(created["eligible_class_ids"]))[0]
    body = {"name": "Hopeful", "class_id": closed, "alignment": "lawful", "ability_roll_id": created["id"]}
    problem = expect(referee.post("/v1/characters", json=body), 422, "character_invalid")
    if "rejections" in problem:
        assert "creation.class.requirements_not_met" in [rejection["code"] for rejection in problem["rejections"]]
    assert expect(referee.get(f"/v1/ability-rolls/{created['id']}"), 200)["used"] is False


def test_a_character_without_a_roll(player: TestClient) -> None:
    stored = create_character_via_api(player, name="Unrolled", class_id="thief", alignment="chaotic")
    character = check_stored(stored, name="Unrolled", class_id="thief")
    assert character.alignment.value == "chaotic"
    assert all(3 <= score <= 18 for score in character.scores.values())
    assert validate_class_choice(character.scores, load_classes().get("thief")) == []
    assert 1 <= character.current_hp == character.max_hp
    check_derived(stored["derived"], character)


def test_derived_values_equal_the_osrlib_character(player: TestClient) -> None:
    stored = create_character_via_api(
        player,
        name="Armoured",
        purchases=[{"item_id": "leather", "quantity": 1}, {"item_id": "dagger", "quantity": 1}],
        equip_item_ids=["leather", "dagger"],
    )
    character = check_stored(stored, name="Armoured", class_id="fighter")
    check_derived(stored["derived"], character)
    assert expect(player.get(f"/v1/characters/{stored['id']}"), 200) == stored


def test_an_adjustment_trades_points_as_osrlib_applies_it(referee: TestClient) -> None:
    fighter = load_classes().get("fighter")
    adjustment = AbilityAdjustment(lowered={AbilityScore.INT: 2}, raised={AbilityScore.STR: 1})

    def legal(candidate: dict[str, Any]) -> bool:
        return not validate_adjustment(scores_of(candidate["scores"]), adjustment, fighter.prime_requisites)

    created = find_roll(referee, legal)
    stored = create_character_via_api(
        referee, ability_roll_id=created["id"], adjustment=adjustment.model_dump(mode="json")
    )
    character = check_stored(stored, name="Cato", class_id="fighter")
    expected = apply_adjustment(scores_of(created["scores"]), adjustment, fighter.prime_requisites)
    assert character.scores == expected


def test_an_illegal_adjustment_is_refused(player: TestClient) -> None:
    # Only STR, INT, and WIS may be lowered, whatever the scores are.
    adjustment = {"lowered": {"dex": 2}, "raised": {"str": 1}}
    refused = player.post(
        "/v1/characters", json={"name": "Cheat", "class_id": "fighter", "alignment": "lawful", "adjustment": adjustment}
    )
    expect(refused, 422, "character_invalid")
    assert expect(player.get("/v1/characters"), 200)["total"] == 0


def test_starting_spells(player: TestClient) -> None:
    stored = create_character_via_api(player, name="Mage", class_id="magic_user", starting_spell_ids=["sleep"])
    character = check_stored(stored, name="Mage", class_id="magic_user")
    assert character.spell_book == ("sleep",)
    fighter = {"name": "Not a caster", "class_id": "fighter", "alignment": "lawful", "starting_spell_ids": ["sleep"]}
    expect(player.post("/v1/characters", json=fighter), 422, "character_invalid")


def test_extra_languages(referee: TestClient) -> None:
    clever = find_roll(referee, lambda candidate: candidate["scores"]["int"] >= 13)
    stored = create_character_via_api(referee, ability_roll_id=clever["id"], extra_languages=["elvish"])
    character = check_stored(stored, name="Cato", class_id="fighter")
    assert character.extra_languages == ("elvish",)
    assert "elvish" in character.languages
    check_derived(stored["derived"], character)

    plain = find_roll(referee, lambda candidate: candidate["scores"]["int"] <= 12)
    body = {
        "name": "Plain",
        "class_id": "fighter",
        "alignment": "lawful",
        "ability_roll_id": plain["id"],
        "extra_languages": ["elvish"],
    }
    expect(referee.post("/v1/characters", json=body), 422, "character_invalid")
    assert expect(referee.get(f"/v1/ability-rolls/{plain['id']}"), 200)["used"] is False


def test_purchases_and_equipment(player: TestClient) -> None:
    # Starting gold is at least 30 gp, and a dagger and leather armour cost 23 gp together.
    stored = create_character_via_api(
        player,
        purchases=[{"item_id": "dagger", "quantity": 1}, {"item_id": "leather", "quantity": 1}],
        equip_item_ids=["dagger", "leather"],
    )
    check_stored(stored, name="Cato", class_id="fighter")
    inventory = stored["character"]["inventory"]
    assert inventory["worn_armour"]["template"]["id"] == "leather"
    assert [item["template"]["id"] for item in inventory["wielded"]] == ["dagger"]
    assert 30 - 23 <= inventory["purse"]["gp"] <= 180 - 23

    unbought = {
        "name": "Empty-handed",
        "class_id": "fighter",
        "alignment": "lawful",
        "purchases": [{"item_id": "dagger", "quantity": 1}],
        "equip_item_ids": ["sword"],
    }
    expect(player.post("/v1/characters", json=unbought), 422, "character_invalid")


def test_the_first_level_hit_point_reroll(referee: TestClient) -> None:
    tables = load_ability_tables()
    for seed in range(1, 9):
        stored = create_character_via_api(referee, hp_reroll_at_first_level=True, seed=seed)
        character = Character.model_validate(stored["character"])
        # With the reroll, the d8 that counts shows 3 or more.
        floor = max(1, 3 + tables.hit_point_modifier(character.scores[AbilityScore.CON]))
        assert character.max_hp >= floor, f"seed {seed}: {character.max_hp} hit points with the reroll"


@pytest.mark.parametrize("value", [None, "evil", 7])
def test_a_malformed_choice_is_422(player: TestClient, value: Any) -> None:
    body = {"name": "Odd", "class_id": "fighter", "alignment": value}
    expect(player.post("/v1/characters", json=body), 422)
    expect(player.post("/v1/characters", json={"name": "Odd", "class_id": "no_such_class", "alignment": "lawful"}), 422)
    assert expect(player.get("/v1/characters"), 200)["total"] == 0


def test_a_player_cannot_choose_the_seed_of_a_character(player: TestClient) -> None:
    refused = player.post(
        "/v1/characters", json={"name": "Lucky", "class_id": "fighter", "alignment": "lawful", "seed": 3}
    )
    expect(refused, 403, "referee_required")
    assert expect(player.get("/v1/characters"), 200)["total"] == 0


def test_a_referee_seed_repeats_a_creation(referee: TestClient) -> None:
    choices = {"purchases": [{"item_id": "dagger", "quantity": 1}], "equip_item_ids": ["dagger"], "seed": 2024}
    first = create_character_via_api(referee, **choices)
    second = create_character_via_api(referee, **choices)
    assert first["id"] != second["id"]
    assert first["character"] == second["character"]
    assert first["derived"] == second["derived"]


def test_import_round_trips_a_character_document(referee: TestClient) -> None:
    document = json_copy(build_party()[0].to_document())
    imported = expect(referee.post("/v1/characters/import", json=document), 201)
    character = check_stored(imported, name="Ash", class_id="fighter")
    assert character == Character.from_document(document)
    check_derived(imported["derived"], character)

    fetched = expect(referee.get(f"/v1/characters/{imported['id']}"), 200)
    exported = json_copy(Character.model_validate(fetched["character"]).to_document())
    again = expect(referee.post("/v1/characters/import", json=exported), 201)
    assert again["id"] != imported["id"]
    assert again["character"] == imported["character"]


def test_import_is_for_referees(player: TestClient) -> None:
    document = json_copy(build_party()[0].to_document())
    expect(player.post("/v1/characters/import", json=document), 403, "referee_required")
    assert expect(player.get("/v1/characters"), 200)["total"] == 0


def test_import_refuses_a_newer_or_malformed_document(referee: TestClient) -> None:
    document: dict[str, Any] = json_copy(build_party()[0].to_document())
    expect(referee.post("/v1/characters/import", json=future_document(document)), 409, "version_unsupported")
    party_kind = dict(document, kind="party")
    expect(referee.post("/v1/characters/import", json=party_kind), 422)
    broken = json_copy(document)
    broken["payload"]["class_id"] = "no_such_class"
    expect(referee.post("/v1/characters/import", json=broken), 422)
    expect(referee.post("/v1/characters/import", json={"name": "Not a document"}), 422)
    assert expect(referee.get("/v1/characters"), 200)["total"] == 0


def test_list_get_and_delete(player: TestClient) -> None:
    made = [create_character_via_api(player, name=name) for name in ("First", "Second", "Third")]
    items = assert_page(expect(player.get("/v1/characters"), 200))
    assert [item["id"] for item in items] == [stored["id"] for stored in reversed(made)], "newest first"
    for item, stored in zip(items, reversed(made), strict=True):
        assert set(item) == CHARACTER_SUMMARY_KEYS, f"a character summary has the keys {sorted(item)}"
        assert item == {
            "id": stored["id"],
            "name": stored["name"],
            "class_id": "fighter",
            "level": 1,
            "max_hp": stored["character"]["max_hp"],
            "created_at": stored["created_at"],
        }

    page = expect(player.get("/v1/characters", params={"limit": 1, "offset": 1}), 200)
    assert assert_page(page, limit=1, offset=1) == [items[1]]
    assert page["total"] == 3
    expect(player.get("/v1/characters", params={"limit": 0}), 422)
    expect(player.get("/v1/characters", params={"limit": 201}), 422)

    middle = made[1]["id"]
    assert expect(player.get(f"/v1/characters/{middle}"), 200) == made[1]
    expect(player.delete(f"/v1/characters/{middle}"), 204)
    expect_not_found(player.get(f"/v1/characters/{middle}"), middle)
    expect_not_found(player.delete(f"/v1/characters/{middle}"), middle)
    remaining = assert_page(expect(player.get("/v1/characters"), 200))
    assert [item["id"] for item in remaining] == [made[2]["id"], made[0]["id"]]


def test_unknown_ids_are_404(player: TestClient) -> None:
    missing_character = "chr_" + "x" * 22
    missing_roll = "rol_" + "x" * 22
    expect_not_found(player.get(f"/v1/characters/{missing_character}"), missing_character)
    expect_not_found(player.delete(f"/v1/characters/{missing_character}"), missing_character)
    expect_not_found(player.get(f"/v1/ability-rolls/{missing_roll}"), missing_roll)
    body = {"name": "Ghost", "class_id": "fighter", "alignment": "lawful", "ability_roll_id": missing_roll}
    expect_not_found(player.post("/v1/characters", json=body), missing_roll)


def test_another_client_cannot_read_or_change_characters_or_rolls(player: TestClient, other: TestClient) -> None:
    created = roll(player)
    stored = create_character_via_api(player)
    unused = roll(player)

    expect_not_found(other.get(f"/v1/characters/{stored['id']}"), stored["id"])
    expect_not_found(other.delete(f"/v1/characters/{stored['id']}"), stored["id"])
    expect_not_found(other.get(f"/v1/ability-rolls/{created['id']}"), created["id"])
    body = {"name": "Thief", "class_id": "fighter", "alignment": "lawful", "ability_roll_id": unused["id"]}
    expect_not_found(other.post("/v1/characters", json=body), unused["id"])
    assert expect(other.get("/v1/characters"), 200)["total"] == 0

    assert expect(player.get(f"/v1/ability-rolls/{unused['id']}"), 200)["used"] is False
    assert expect(player.get(f"/v1/characters/{stored['id']}"), 200) == stored
