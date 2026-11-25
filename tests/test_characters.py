import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi.testclient import TestClient
from osrlib.core.abilities import AbilityAdjustment, AbilityScore, apply_adjustment, validate_adjustment
from osrlib.core.alignment import Alignment
from osrlib.core.character import (
    CHARACTER_CREATION_STREAM,
    Character,
    CharacterCreationResult,
    create_character,
    roll_ability_scores,
    validate_class_choice,
)
from osrlib.core.classes import ClassDefinition, level_title, xp_modifier_pct
from osrlib.core.items import ItemInstance, MagicItemCategory, validate_equip
from osrlib.core.rng import RngStreams
from osrlib.core.ruleset import Ruleset
from osrlib.core.spells import caster_profile
from osrlib.data import load_ability_tables, load_classes, load_equipment, load_languages, load_magic_items, load_spells

from bxapi.db import CharacterDocumentRow, Database
from bxapi.settings import Settings

from .conftest import problem_without_id

FIGHTER = {"name": "Hild", "class_id": "fighter", "alignment": "lawful"}
CLASS_IDS = [definition.id for definition in load_classes().classes]
MAX_OFFSET = 2**53 - 1
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


def osrlib_character(
    seed: int, class_id: str = "fighter", *, hp_reroll: bool = False, **choices: Any
) -> CharacterCreationResult:
    return create_character(
        name="Hild",
        class_id=class_id,
        alignment=Alignment.LAWFUL,
        ruleset=Ruleset(hp_reroll_at_first_level=hp_reroll),
        stream=RngStreams(master_seed=seed).get(CHARACTER_CREATION_STREAM),
        **choices,
    )


def osrlib_eligible(scores: dict[str, int]) -> list[str]:
    table = {AbilityScore(key): value for key, value in scores.items()}
    return [definition.id for definition in load_classes().classes if not validate_class_choice(table, definition)]


def legal_choices(definition: ClassDefinition, seed: int) -> dict[str, Any] | None:
    """Return osrlib's arguments for a character that uses every choice, or `None` if this seed can't have one.

    The choices are a legal adjustment, an extra language, a first spell for an arcane caster, a
    weapon the class may wield, and a torch.
    """
    scores = roll_ability_scores(RngStreams(master_seed=seed).get(CHARACTER_CREATION_STREAM)).scores
    if validate_class_choice(scores, definition):
        return None
    primes, fixed = definition.prime_requisites, definition.may_not_lower
    adjustment = None
    for lowered in (AbilityScore.WIS, AbilityScore.STR, AbilityScore.INT):
        for raised in primes:
            candidate = AbilityAdjustment(lowered={lowered: 2}, raised={raised: 1})
            if validate_adjustment(scores, candidate, primes, fixed):
                continue
            adjusted = apply_adjustment(scores, candidate, primes, fixed)
            if load_ability_tables().additional_languages(adjusted[AbilityScore.INT]) >= 1:
                adjustment = candidate
    if adjustment is None:
        return None
    language = next(
        entry.id for entry in load_languages().languages if entry.choosable and entry.id not in definition.languages
    )
    profile = caster_profile(definition)
    spells = [load_spells().by_list(profile.spell_list, 1)[0].id] if profile and profile.kind == "arcane" else []
    weapon = next(
        template
        for template in load_equipment().weapons
        if template.cost_gp <= 10 and not validate_equip(definition, ItemInstance(template=template))
    )
    return {
        "adjustment": adjustment,
        "starting_spell_ids": spells,
        "extra_languages": [language],
        "purchases": [(weapon.id, 1), ("torch", 1)],
        "equip_ids": [weapon.id],
    }


def request_body(class_id: str, choices: dict[str, Any], *, hp_reroll: bool) -> dict[str, Any]:
    """Return the API's body for the osrlib arguments `legal_choices` returns."""
    return FIGHTER | {
        "class_id": class_id,
        "adjustment": choices["adjustment"].model_dump(mode="json"),
        "starting_spell_ids": choices["starting_spell_ids"],
        "extra_languages": choices["extra_languages"],
        "purchases": [{"item_id": item_id, "quantity": lots} for item_id, lots in choices["purchases"]],
        "equip_item_ids": choices["equip_ids"],
        "hp_reroll_at_first_level": hp_reroll,
    }


def full_cases(class_id: str, *, hp_reroll: bool, count: int = 3) -> list[tuple[int, dict[str, Any]]]:
    """Return seeds, with their choices, for which osrlib creates the character with every choice.

    With `hp_reroll`, each seed's first hit die shows 1 or 2, so the reroll changes the hit points.
    """
    definition = load_classes().get(class_id)
    found: list[tuple[int, dict[str, Any]]] = []
    for seed in range(5000):
        choices = legal_choices(definition, seed)
        if choices is None:
            continue
        try:
            result = osrlib_character(seed, class_id, hp_reroll=hp_reroll, **choices)
        except ValueError:
            continue
        if hp_reroll and len(result.hit_point_roll.rolls) == 1:
            continue
        found.append((seed, choices))
        if len(found) == count:
            return found
    raise AssertionError(f"no {count} seeds below 5000 give a {class_id} every choice")


def rolled_scores(seed: int) -> dict[str, int]:
    rolled = roll_ability_scores(RngStreams(master_seed=seed).get(CHARACTER_CREATION_STREAM))
    return {ability.value: score for ability, score in rolled.scores.items()}


def rolled_dice(seed: int) -> dict[str, Any]:
    """Return osrlib's `AbilityScoreRolls` for a seed, as JSON."""
    return roll_ability_scores(RngStreams(master_seed=seed).get(CHARACTER_CREATION_STREAM)).model_dump(mode="json")


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
    assert set(body) == {"id", "scores", "rolls", "eligible_class_ids", "used", "created_at"}
    assert body["id"].startswith("rol_")
    assert body["scores"] == rolled_scores(7)
    assert body["rolls"] == rolled_dice(7)
    assert referee.get(f"/v1/ability-rolls/{body['id']}").json()["rolls"] == body["rolls"]
    assert body["eligible_class_ids"] == osrlib_eligible(body["scores"])
    assert body["used"] is False
    assert "seed" not in body
    assert "seed" not in referee.get(f"/v1/ability-rolls/{body['id']}").json()


def test_eligible_classes_come_from_osrlib(referee: TestClient) -> None:
    every = set(CLASS_IDS)
    narrowed = 0
    for seed in range(60):
        body = referee.post("/v1/ability-rolls", json={"seed": seed}).json()
        assert body["eligible_class_ids"] == osrlib_eligible(body["scores"])
        narrowed += set(body["eligible_class_ids"]) != every
    assert narrowed, "no seed rolled scores that miss a class's requirements"


def test_a_player_rolls_without_a_body_but_not_with_a_seed(player: TestClient) -> None:
    assert player.post("/v1/ability-rolls").status_code == 201
    assert player.post("/v1/ability-rolls", json={}).status_code == 201
    refused = player.post("/v1/ability-rolls", json={"seed": 1})
    assert (refused.status_code, refused.json()["code"]) == (403, "referee_required")


def test_the_dice_of_a_roll_add_up_to_its_scores_and_match_the_preview(player: TestClient) -> None:
    roll = player.post("/v1/ability-rolls").json()
    assert roll["rolls"]["scores"] == roll["scores"]
    assert {ability: sum(dice) for ability, dice in roll["rolls"]["rolls"].items()} == roll["scores"]
    assert all(1 <= die <= 6 for dice in roll["rolls"]["rolls"].values() for die in dice)
    body = {"name": "Brenna", "class_id": "fighter", "alignment": "lawful"}
    # A fighter has no minimum scores, so every roll can make one.
    previewed = preview(player, roll["id"], body)
    assert previewed.status_code == 200, previewed.text
    assert previewed.json()["rolls"]["ability_rolls"] == roll["rolls"]
    assert player.post("/v1/characters", json=body | {"ability_roll_id": roll["id"]}).status_code == 201
    assert player.get(f"/v1/ability-rolls/{roll['id']}").json() == roll | {"used": True}


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


@pytest.mark.parametrize("path", ["generate", "ability roll"])
@pytest.mark.parametrize("hp_reroll", [False, True])
@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_every_choice_matches_osrlib_create_character(
    referee: TestClient, class_id: str, hp_reroll: bool, path: str
) -> None:
    for seed, choices in full_cases(class_id, hp_reroll=hp_reroll):
        expected = osrlib_character(seed, class_id, hp_reroll=hp_reroll, **choices).character
        body = request_body(class_id, choices, hp_reroll=hp_reroll)
        if path == "generate":
            response = referee.post("/v1/generate/character", json=body | {"seed": seed})
            assert response.status_code == 200, response.text
        else:
            roll = referee.post("/v1/ability-rolls", json={"seed": seed}).json()
            response = referee.post("/v1/characters", json=body | {"ability_roll_id": roll["id"]})
            assert response.status_code == 201, response.text
        made = response.json()
        assert made["character"] == expected.model_dump(mode="json"), f"seed {seed}"
        assert made["derived"] == expected_derived(expected)


def test_a_roll_decides_the_hit_points_and_the_gold(referee: TestClient) -> None:
    for seed in range(20):
        roll = referee.post("/v1/ability-rolls", json={"seed": seed}).json()
        # The server rolls the scores again from the stored seed, and gets the stored scores.
        assert roll["scores"] == rolled_scores(seed)
        if "fighter" not in roll["eligible_class_ids"]:
            continue
        stored = referee.post("/v1/characters", json=FIGHTER | {"ability_roll_id": roll["id"]}).json()
        expected = osrlib_character(seed).character
        assert stored["character"] == expected.model_dump(mode="json")
        assert stored["character"]["inventory"]["purse"]["gp"] == expected.inventory.purse.gp
        assert stored["character"]["max_hp"] == expected.max_hp


def test_retrying_a_refused_creation_never_changes_the_gold(referee: TestClient) -> None:
    # A player who could retry for a new gold roll would ask for gear only a high roll covers.
    seed = 3
    gold = osrlib_character(seed).gold_roll.total
    dagger = load_equipment().get("dagger").cost_gp
    roll = referee.post("/v1/ability-rolls", json={"seed": seed}).json()
    greedy = FIGHTER | {
        "ability_roll_id": roll["id"],
        "purchases": [{"item_id": "dagger", "quantity": gold // dagger + 1}],
    }
    refusals = [referee.post("/v1/characters", json=greedy) for _ in range(100)]
    assert {response.status_code for response in refusals} == {422}
    assert len({json.dumps(problem_without_id(response)) for response in refusals}) == 1
    affordable = greedy | {"purchases": [{"item_id": "dagger", "quantity": gold // dagger}]}
    stored = referee.post("/v1/characters", json=affordable).json()
    assert stored["character"]["inventory"]["purse"]["gp"] == gold - (gold // dagger) * dagger


def test_a_seed_with_an_ability_roll_is_a_validation_error(referee: TestClient) -> None:
    roll = referee.post("/v1/ability-rolls").json()
    response = referee.post("/v1/characters", json=FIGHTER | {"ability_roll_id": roll["id"], "seed": 5})
    assert (response.status_code, response.json()["code"]) == (422, "validation_error")
    assert referee.get(f"/v1/ability-rolls/{roll['id']}").json()["used"] is False


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
    ("choices", "members"),
    [
        ({"class_id": "no_such_class"}, {"field": "class_id", "id": "no_such_class"}),
        (
            {"purchases": [{"item_id": "dagger"}, {"item_id": "no_such_item"}]},
            {"field": "purchases", "index": 1, "id": "no_such_item"},
        ),
        (
            {"purchases": [{"item_id": "dagger"}], "equip_item_ids": ["dagger", "sword"]},
            {"field": "equip_item_ids", "index": 1, "id": "sword"},
        ),
    ],
)
def test_an_input_osrlib_has_no_rejection_for_is_located(
    player: TestClient, choices: dict[str, Any], members: dict[str, Any]
) -> None:
    problem = player.post("/v1/characters", json=FIGHTER | choices).json()
    assert problem["code"] == "character_invalid"
    assert "rejections" not in problem
    assert {key: problem.get(key) for key in ("field", "index", "id")} == {"index": None} | members


@pytest.mark.parametrize(
    ("purchases", "index"),
    [([{"item_id": "dagger", "quantity": 2}], 1), ([{"item_id": "dagger"}, {"item_id": "dagger"}], 2)],
)
def test_equipping_more_than_was_bought_says_so(
    player: TestClient, purchases: list[dict[str, Any]], index: int
) -> None:
    body = FIGHTER | {"purchases": purchases, "equip_item_ids": ["dagger", "dagger", "dagger"]}
    problem = player.post("/v1/characters", json=body).json()
    assert (problem["code"], problem["field"], problem["index"], problem["id"]) == (
        "character_invalid",
        "equip_item_ids",
        index,
        "dagger",
    )
    assert "more times than the purchases buy it" in problem["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"class_id": "fighter", "alignment": "lawful"},
        FIGHTER | {"name": ""},
        FIGHTER | {"alignment": "evil"},
        FIGHTER | {"purchases": [{"item_id": "dagger", "quantity": 0}]},
        FIGHTER | {"purchases": [{"item_id": "dagger", "quantity": 10_001}]},
        FIGHTER | {"purchases": [{"item_id": "dagger", "quantity": 10**30}]},
        FIGHTER | {"name": "x" * 101},
        FIGHTER | {"name": "x" * 60_000},
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


# Previews


def preview(caller: TestClient, roll_id: str, body: dict[str, Any]) -> Any:
    return caller.post(f"/v1/ability-rolls/{roll_id}/preview", json=body)


def all_classes_seed() -> int:
    """Return a seed whose scores meet every class's requirements."""
    return seed_where(lambda scores: osrlib_eligible(scores) == CLASS_IDS)


@pytest.mark.parametrize("hp_reroll", [False, True])
@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_a_preview_is_what_creation_then_stores(referee: TestClient, class_id: str, hp_reroll: bool) -> None:
    for seed, choices in full_cases(class_id, hp_reroll=hp_reroll):
        result = osrlib_character(seed, class_id, hp_reroll=hp_reroll, **choices)
        body = request_body(class_id, choices, hp_reroll=hp_reroll)
        roll = referee.post("/v1/ability-rolls", json={"seed": seed}).json()
        response = preview(referee, roll["id"], body)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "character": result.character.model_dump(mode="json"),
            "derived": expected_derived(result.character),
            "rolls": {
                "ability_rolls": result.ability_rolls.model_dump(mode="json"),
                "hit_point_roll": result.hit_point_roll.model_dump(mode="json"),
                "gold_roll": result.gold_roll.model_dump(mode="json"),
            },
        }, f"seed {seed}"
        assert referee.get(f"/v1/ability-rolls/{roll['id']}").json()["used"] is False
        stored = referee.post("/v1/characters", json=body | {"ability_roll_id": roll["id"]}).json()
        assert stored["character"] == response.json()["character"]
        assert stored["derived"] == response.json()["derived"]


def test_a_preview_without_purchases_shows_the_gold_creation_starts_with(player: TestClient) -> None:
    roll = player.post("/v1/ability-rolls").json()
    shown = preview(player, roll["id"], FIGHTER).json()
    gold = shown["rolls"]["gold_roll"]["total"]
    assert shown["character"]["inventory"]["purse"]["gp"] == gold
    assert shown["character"]["max_hp"] == shown["rolls"]["hit_point_roll"]["hit_points"]
    assert shown["rolls"]["ability_rolls"]["scores"] == roll["scores"]
    dagger = load_equipment().get("dagger").cost_gp
    body = FIGHTER | {"ability_roll_id": roll["id"], "purchases": [{"item_id": "dagger", "quantity": gold // dagger}]}
    stored = player.post("/v1/characters", json=body).json()
    assert stored["character"]["inventory"]["purse"]["gp"] == gold - (gold // dagger) * dagger
    assert stored["character"]["max_hp"] == shown["character"]["max_hp"]


def test_previews_are_equal_and_store_nothing(player: TestClient) -> None:
    roll = player.post("/v1/ability-rolls").json()
    used_bytes = player.get("/v1/meta").json()["storage"]["used_bytes"]
    previews = [preview(player, roll["id"], FIGHTER | GEAR) for _ in range(5)]
    assert {response.status_code for response in previews} == {200}
    assert len({json.dumps(response.json()) for response in previews}) == 1
    assert set(previews[0].json()) == {"character", "derived", "rolls"}
    assert previews[0].json()["character"]["id"] is None
    assert player.get("/v1/meta").json()["storage"]["used_bytes"] == used_bytes
    assert player.get("/v1/characters").json()["total"] == 0
    assert player.get(f"/v1/ability-rolls/{roll['id']}").json() == roll


@pytest.mark.parametrize(
    "choices",
    [
        {"name": "Durin", "class_id": "dwarf"},
        {"class_id": "magic_user"},
        {"starting_spell_ids": ["sleep"]},
        {"adjustment": {"lowered": {"dex": 2}, "raised": {"str": 1}}},
        {"extra_languages": ["no_such_language"]},
        {"purchases": [{"item_id": "plate_mail", "quantity": 4}]},
        {"class_id": "cleric", "purchases": [{"item_id": "sword"}], "equip_item_ids": ["sword"]},
        {"class_id": "no_such_class"},
        {"purchases": [{"item_id": "dagger"}, {"item_id": "no_such_item"}]},
        {"purchases": [{"item_id": "dagger"}], "equip_item_ids": ["dagger", "sword"]},
        {"purchases": [{"item_id": "dagger", "quantity": 2}], "equip_item_ids": ["dagger", "dagger"]},
    ],
)
def test_a_refused_preview_is_the_refused_creation(referee: TestClient, choices: dict[str, Any]) -> None:
    seed = seed_where(lambda scores: scores["con"] < 9) if choices.get("class_id") == "dwarf" else all_classes_seed()
    roll = referee.post("/v1/ability-rolls", json={"seed": seed}).json()
    refused = preview(referee, roll["id"], FIGHTER | choices)
    assert (refused.status_code, refused.json()["code"]) == (422, "character_invalid")
    assert referee.get(f"/v1/ability-rolls/{roll['id']}").json()["used"] is False
    created = referee.post("/v1/characters", json=FIGHTER | choices | {"ability_roll_id": roll["id"]})
    assert created.status_code == 422
    assert problem_without_id(refused) == problem_without_id(created)
    assert referee.get(f"/v1/ability-rolls/{roll['id']}").json()["used"] is False


def test_a_preview_of_another_clients_roll_or_an_unknown_roll_is_not_found(
    player: TestClient, other: TestClient
) -> None:
    roll = player.post("/v1/ability-rolls").json()
    stolen = preview(other, roll["id"], FIGHTER)
    assert stolen.status_code == 404
    assert (stolen.json()["code"], stolen.json()["resource"], stolen.json()["id"]) == (
        "not_found",
        "ability roll",
        roll["id"],
    )
    assert preview(player, "rol_5mJq0nY2bXr8TzW4cH1eKd", FIGHTER).json()["code"] == "not_found"
    assert player.get(f"/v1/ability-rolls/{roll['id']}").json()["used"] is False


def test_a_preview_of_a_used_roll_is_a_conflict(player: TestClient) -> None:
    roll = player.post("/v1/ability-rolls").json()
    assert player.post("/v1/characters", json=FIGHTER | {"ability_roll_id": roll["id"]}).status_code == 201
    used = preview(player, roll["id"], FIGHTER)
    assert (used.status_code, used.json()["code"]) == (409, "ability_roll_used")
    again = player.post("/v1/characters", json=FIGHTER | {"ability_roll_id": roll["id"]})
    assert problem_without_id(used) == problem_without_id(again)


@pytest.mark.parametrize(
    "extra",
    [{"seed": 5}, {"ability_roll_id": "rol_5mJq0nY2bXr8TzW4cH1eKd"}, {"ability_roll_id": None}, {"dry_run": True}],
)
def test_a_preview_body_with_a_member_creation_doesnt_take_here_is_a_validation_error(
    referee: TestClient, extra: dict[str, Any]
) -> None:
    roll = referee.post("/v1/ability-rolls").json()
    response = preview(referee, roll["id"], FIGHTER | extra)
    assert (response.status_code, response.json()["code"]) == (422, "validation_error")
    assert response.json()["errors"][0]["type"] == "extra_forbidden"
    assert referee.get(f"/v1/ability-rolls/{roll['id']}").json()["used"] is False


def test_a_malformed_preview_body_is_a_validation_error(player: TestClient) -> None:
    roll = player.post("/v1/ability-rolls").json()
    for body in ({"class_id": "fighter", "alignment": "lawful"}, FIGHTER | {"alignment": "evil"}, [FIGHTER]):
        response = player.post(f"/v1/ability-rolls/{roll['id']}/preview", json=body)
        assert (response.status_code, response.json()["code"]) == (422, "validation_error")


def single_choice_variants(class_id: str, choices: dict[str, Any], *, hp_reroll: bool) -> list[dict[str, Any]]:
    """Return request bodies that each differ from the fewest legal choices in one choice that takes no dice."""
    full = request_body(class_id, choices, hp_reroll=hp_reroll)
    base = FIGHTER | {
        "class_id": class_id,
        "starting_spell_ids": choices["starting_spell_ids"],
        "hp_reroll_at_first_level": hp_reroll,
    }
    # The extra language is legal only with the adjustment, which raises a score that can add a language.
    variants = [
        base | {"adjustment": full["adjustment"]},
        base | {"adjustment": full["adjustment"], "extra_languages": full["extra_languages"]},
        base | {"purchases": full["purchases"]},
        base | {"purchases": full["purchases"], "equip_item_ids": full["equip_item_ids"]},
        base | {"name": "Someone else"},
        base | {"alignment": "chaotic"},
        full,
    ]
    profile = caster_profile(load_classes().get(class_id))
    if choices["starting_spell_ids"] and profile is not None:
        other_spell = load_spells().by_list(profile.spell_list, 1)[-1].id
        assert other_spell not in choices["starting_spell_ids"]
        variants.append(base | {"starting_spell_ids": [other_spell]})
    return [base, *variants]


@pytest.mark.parametrize("hp_reroll", [False, True])
@pytest.mark.parametrize("class_id", CLASS_IDS)
def test_only_the_class_and_the_reroll_option_change_the_hit_point_and_gold_rolls(
    referee: TestClient, class_id: str, hp_reroll: bool
) -> None:
    for seed, choices in full_cases(class_id, hp_reroll=hp_reroll):
        roll = referee.post("/v1/ability-rolls", json={"seed": seed}).json()
        shown = []
        for body in single_choice_variants(class_id, choices, hp_reroll=hp_reroll):
            response = preview(referee, roll["id"], body)
            assert response.status_code == 200, response.text
            rolls = response.json()["rolls"]
            shown.append((rolls["hit_point_roll"], rolls["gold_roll"]))
        assert len({json.dumps(rolls) for rolls in shown}) == 1, f"seed {seed}"


def test_the_class_and_the_reroll_option_can_change_the_gold(referee: TestClient) -> None:
    # A d6 sometimes discards a raw draw where a d8 never does, and a reroll takes more draws, so the gold can move.
    by_class, by_reroll = False, False
    for seed in range(200):
        roll = referee.post("/v1/ability-rolls", json={"seed": seed}).json()
        if not {"fighter", "cleric"} <= set(roll["eligible_class_ids"]):
            continue
        fighter = preview(referee, roll["id"], FIGHTER).json()["rolls"]["gold_roll"]
        cleric = preview(referee, roll["id"], FIGHTER | {"class_id": "cleric"}).json()["rolls"]["gold_roll"]
        rerolled = preview(referee, roll["id"], FIGHTER | {"hp_reroll_at_first_level": True}).json()["rolls"]
        by_class = by_class or fighter != cleric
        by_reroll = by_reroll or (len(rerolled["hit_point_roll"]["rolls"]) > 1 and rerolled["gold_roll"] != fighter)
        if by_class and by_reroll:
            return
    raise AssertionError("no seed below 200 changes the gold with the class and with the reroll option")


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
    document: dict[str, Any] = osrlib_character(4).character.to_document()
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
        ("/v1/ability-rolls/{roll_id}/preview", "post"): "preview_character",
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


def test_the_largest_name_and_quantity_reach_osrlib(player: TestClient) -> None:
    assert player.post("/v1/characters", json=FIGHTER | {"name": "x" * 100}).status_code == 201
    costly = FIGHTER | {"purchases": [{"item_id": "dagger", "quantity": 10_000}]}
    assert player.post("/v1/characters", json=costly).json()["code"] == "character_invalid"


def test_a_name_of_spaces_is_kept_as_osrlib_keeps_it(player: TestClient) -> None:
    assert osrlib_character(1).character.model_copy(update={"name": "   "}).name == "   "
    stored = player.post("/v1/characters", json=FIGHTER | {"name": "   "})
    assert stored.status_code == 201
    assert stored.json()["character"]["name"] == "   "


# Imports that osrlib's model accepts but the server refuses


def magic_item(template_id: str, **fields: Any) -> dict[str, Any]:
    return {"instance_type": "magic_item", "instance_id": "magic-item-0001", "template_id": template_id, **fields}


def magic_armour_id() -> str:
    """Return a magic armour whose armour class osrlib computes from its `base_item_id`."""
    return next(
        template.id
        for template in load_magic_items().items
        if template.category is MagicItemCategory.ARMOUR and template.ac_set is None
    )


@pytest.mark.parametrize(
    ("slot", "value"),
    [
        ("worn_armour", magic_item("no_such_item")),
        ("rings", [magic_item("no_such_ring")]),
        ("worn_armour", magic_item(magic_armour_id(), base_item_id="no_such_armour")),
    ],
)
def test_an_import_osrlib_cant_compute_values_for_stores_nothing(referee: TestClient, slot: str, value: Any) -> None:
    document: dict[str, Any] = osrlib_character(4).character.to_document()
    document["payload"]["inventory"][slot] = value
    Character.from_document(document)
    response = referee.post("/v1/characters/import", json=document)
    assert (response.status_code, response.json()["code"]) == (422, "content_invalid")
    assert referee.get("/v1/characters").json()["total"] == 0


@pytest.mark.parametrize(
    "fields",
    [
        {"conditions": [{"condition": "asleep", "effect_id": "effect-0001"}]},
        {"stat_modifiers": [{"kind": "ac_bonus", "value": 2, "effect_id": "effect-0001"}]},
    ],
)
def test_an_import_with_effects_from_another_game_is_refused(referee: TestClient, fields: dict[str, Any]) -> None:
    document: dict[str, Any] = osrlib_character(4).character.to_document()
    document["payload"] |= fields
    Character.from_document(document)
    response = referee.post("/v1/characters/import", json=document)
    assert (response.status_code, response.json()["code"]) == (422, "content_invalid")
    assert referee.get("/v1/characters").json()["total"] == 0


def test_an_import_with_hit_points_the_database_cannot_store_is_refused(referee: TestClient) -> None:
    document: dict[str, Any] = osrlib_character(4).character.to_document()
    document["payload"]["max_hp"] = 2**63
    Character.from_document(document)
    response = referee.post("/v1/characters/import", json=document)
    assert (response.status_code, response.json()["code"]) == (422, "content_invalid")
    assert referee.get("/v1/characters").json()["total"] == 0
    document["payload"]["max_hp"] = 2**63 - 1
    assert referee.post("/v1/characters/import", json=document).status_code == 201


def test_an_import_accepts_what_osrlibs_model_accepts(referee: TestClient) -> None:
    veteran = osrlib_character(4).character.model_copy(update={"level": 14, "xp": 2_400_000})
    document: dict[str, Any] = veteran.to_document()
    document["payload"]["conditions"] = [{"condition": "dead", "effect_id": None}]
    response = referee.post("/v1/characters/import", json=document)
    assert response.status_code == 201, response.text
    assert response.json()["level"] == 14
    assert response.json()["character"]["conditions"] == [{"condition": "dead", "effect_id": None}]


# Offsets


@pytest.mark.parametrize("path", ["/v1/characters", "/v1/rules/classes", "/v1/adventures"])
def test_the_largest_offset_is_an_empty_page(player: TestClient, path: str) -> None:
    page = player.get(path, params={"offset": MAX_OFFSET})
    assert page.status_code == 200
    assert (page.json()["items"], page.json()["offset"]) == ([], MAX_OFFSET)
    too_far = player.get(path, params={"offset": MAX_OFFSET + 1})
    assert (too_far.status_code, too_far.json()["code"]) == (422, "validation_error")
    assert player.get(path, params={"offset": 10**30}).json()["code"] == "validation_error"
