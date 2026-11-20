import json
from collections.abc import Callable
from importlib import resources
from typing import Any

import pytest
from fastapi.testclient import TestClient
from osrlib.core.spells import caster_profile
from osrlib.crawl.commands import ALL_COMMAND_CLASSES, SessionMode
from osrlib.data import (
    load_ability_tables,
    load_classes,
    load_combat_tables,
    load_encounter_tables,
    load_equipment,
    load_languages,
    load_magic_items,
    load_monsters,
    load_spells,
    load_treasure_tables,
)
from pydantic import BaseModel, TypeAdapter

from bxapi.commands import PLAY_COMMAND_TYPES

CACHE_CONTROL = "private, max-age=3600"

# Each catalog: the list path (the detail path adds `/{id}`) and a function that returns the catalog's entries.
CATALOGS: dict[str, tuple[str, Callable[[], list[Any]]]] = {
    "classes": ("/v1/rules/classes", lambda: list(load_classes().classes)),
    "monsters": ("/v1/rules/monsters", lambda: list(load_monsters().monsters)),
    "spells": ("/v1/rules/spells", lambda: list(load_spells().spells)),
    "equipment": (
        "/v1/rules/equipment",
        lambda: [
            *load_equipment().weapons,
            *load_equipment().armour,
            *load_equipment().gear,
            *load_equipment().ammunition,
        ],
    ),
    "magic-items": ("/v1/rules/magic-items", lambda: list(load_magic_items().items)),
}


def dumped(model: BaseModel) -> Any:
    return json.loads(model.model_dump_json())


def every_page(client: TestClient, path: str) -> list[dict[str, Any]]:
    """Read every page of a list, with the query string in `path`, and check `total` on the way."""
    separator = "&" if "?" in path else "?"
    items: list[dict[str, Any]] = []
    while True:
        response = client.get(f"{path}{separator}limit=200&offset={len(items)}")
        assert response.status_code == 200
        body = response.json()
        items.extend(body["items"])
        if len(items) >= body["total"] or not body["items"]:
            assert len(items) == body["total"]
            return items


@pytest.mark.parametrize("catalog", CATALOGS)
def test_list_has_the_catalog_in_order(client: TestClient, catalog: str) -> None:
    path, entries = CATALOGS[catalog]
    items = every_page(client, path)
    assert [item["id"] for item in items] == [entry.id for entry in entries()]
    assert [item["name"] for item in items] == [entry.name for entry in entries()]


@pytest.mark.parametrize("catalog", CATALOGS)
def test_every_listed_id_resolves_at_its_detail_endpoint(client: TestClient, catalog: str) -> None:
    path, entries = CATALOGS[catalog]
    items = every_page(client, path)
    assert len(items) > 0
    for item, entry in zip(items, entries(), strict=True):
        response = client.get(f"{path}/{item['id']}")
        assert response.status_code == 200, item["id"]
        assert response.json() == dumped(entry)


@pytest.mark.parametrize("catalog", CATALOGS)
def test_unknown_id_is_not_found(client: TestClient, catalog: str) -> None:
    path, _ = CATALOGS[catalog]
    response = client.get(f"{path}/no_such_id")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["id"] == "no_such_id"
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(
    ("catalog", "resource"),
    [
        ("classes", "class"),
        ("monsters", "monster"),
        ("spells", "spell"),
        ("equipment", "equipment item"),
        ("magic-items", "magic item"),
    ],
)
def test_not_found_names_the_resource(client: TestClient, catalog: str, resource: str) -> None:
    path, _ = CATALOGS[catalog]
    assert client.get(f"{path}/no_such_id").json()["resource"] == resource


@pytest.mark.parametrize("catalog", CATALOGS)
def test_paging(client: TestClient, catalog: str) -> None:
    path, entries = CATALOGS[catalog]
    ids = [entry.id for entry in entries()]
    body = client.get(path, params={"limit": 3, "offset": 2}).json()
    assert body["total"] == len(ids)
    assert (body["limit"], body["offset"]) == (3, 2)
    assert [item["id"] for item in body["items"]] == ids[2:5]
    past_end = client.get(path, params={"offset": len(ids)}).json()
    assert past_end["items"] == []
    assert past_end["total"] == len(ids)
    assert client.get(path).json()["limit"] == 50


@pytest.mark.parametrize("catalog", CATALOGS)
def test_paging_rejects_a_bad_limit(client: TestClient, catalog: str) -> None:
    path, _ = CATALOGS[catalog]
    assert client.get(path, params={"limit": 0}).status_code == 422
    assert client.get(path, params={"limit": 201}).status_code == 422
    assert client.get(path, params={"offset": -1}).status_code == 422


def test_class_summary_fields(client: TestClient) -> None:
    items = every_page(client, "/v1/rules/classes")
    for item, definition in zip(items, load_classes().classes, strict=True):
        assert item == {
            "id": definition.id,
            "name": definition.name,
            "race": definition.race,
            "prime_requisites": dumped_value(definition.prime_requisites),
            "requirements": dumped_value(definition.requirements),
            "hit_die": definition.hit_die,
            "max_level": definition.max_level,
        }
    fighter = client.get("/v1/rules/classes/fighter").json()
    assert fighter["id"] == "fighter"
    assert fighter["name"] == "Fighter"


def dumped_value(value: Any) -> Any:
    return json.loads(TypeAdapter(type(value)).dump_json(value))


def test_monster_summary_fields_for_the_whole_catalog(client: TestClient) -> None:
    items = every_page(client, "/v1/rules/monsters")
    assert any(item["ac"] is None for item in items), "the catalog has a monster with no armour class"
    for item, template in zip(items, load_monsters().monsters, strict=True):
        assert item == {
            "id": template.id,
            "name": template.name,
            "hit_dice": dumped(template.hit_dice),
            "ac": template.ac,
            "ac_ascending": template.ac_ascending,
            "xp": template.xp,
            "alignment": dumped(template.alignment),
        }


def test_q_matches_a_name_that_the_id_doesnt_contain(client: TestClient) -> None:
    assert [item["id"] for item in every_page(client, "/v1/rules/monsters?q=gänger")] == ["doppelganger"]
    assert [item["id"] for item in every_page(client, "/v1/rules/monsters?q=APE, WHITE")] == ["ape_white"]


def test_q_normalizes_unicode_before_comparing(client: TestClient) -> None:
    decomposed = "Doppelga\u0308nger"
    assert [item["id"] for item in every_page(client, f"/v1/rules/monsters?q={decomposed}")] == ["doppelganger"]
    assert [item["id"] for item in every_page(client, "/v1/rules/monsters?q=DOPPELGÄNGER")] == ["doppelganger"]


def test_q_matches_id_or_name_ignoring_case(client: TestClient) -> None:
    items = every_page(client, "/v1/rules/monsters?q=GOBLIN")
    expected = [m.id for m in load_monsters().monsters if "goblin" in m.id.lower() or "goblin" in m.name.lower()]
    assert [item["id"] for item in items] == expected
    assert "goblin" in expected
    assert client.get("/v1/rules/monsters", params={"q": "no such monster anywhere"}).json()["total"] == 0
    spell = every_page(client, "/v1/rules/spells?q=MISSILE")
    assert "magic_missile" in [item["id"] for item in spell]
    assert "sword" in [item["id"] for item in every_page(client, "/v1/rules/equipment?q=SWORD")]
    assert "potion_of_healing" in [item["id"] for item in every_page(client, "/v1/rules/magic-items?q=HEALING")]


def test_spell_summary_fields(client: TestClient) -> None:
    items = every_page(client, "/v1/rules/spells")
    for item, spell in zip(items, load_spells().spells, strict=True):
        assert item == {
            "id": spell.id,
            "name": spell.name,
            "level": spell.level,
            "spell_list": spell.spell_list,
            "range": spell.range,
            "duration": spell.duration,
        }


def test_spell_filters(client: TestClient) -> None:
    catalog = load_spells()
    by_level = every_page(client, "/v1/rules/spells?level=2")
    assert [item["id"] for item in by_level] == [s.id for s in catalog.spells if s.level == 2]

    clerics = every_page(client, "/v1/rules/spells?class_id=cleric")
    assert [item["id"] for item in clerics] == [s.id for s in catalog.by_list("cleric")]

    profile = caster_profile(load_classes().get("elf"))
    assert profile is not None
    elf = every_page(client, "/v1/rules/spells?class_id=elf")
    assert [item["id"] for item in elf] == [s.id for s in catalog.by_list(profile.spell_list)]
    assert elf == every_page(client, "/v1/rules/spells?class_id=magic_user")

    both = every_page(client, "/v1/rules/spells?class_id=cleric&level=1&q=light")
    assert both
    assert [item["id"] for item in both] == [
        s.id for s in catalog.by_list("cleric", 1) if "light" in s.id.lower() or "light" in s.name.lower()
    ]


def test_spell_filters_that_match_nothing_return_an_empty_page(client: TestClient) -> None:
    for query in ("class_id=fighter", "q=zzzzzz"):
        body = client.get(f"/v1/rules/spells?{query}").json()
        assert body["items"] == []
        assert body["total"] == 0


def test_spell_class_filter_with_an_unknown_class_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/rules/spells?class_id=no_such_class")
    assert response.status_code == 404
    assert response.json()["resource"] == "class"
    assert response.json()["id"] == "no_such_class"


def test_spell_level_must_be_a_spell_level(client: TestClient) -> None:
    levels = {spell.level for spell in load_spells().spells}
    assert client.get(f"/v1/rules/spells?level={min(levels)}").status_code == 200
    assert client.get(f"/v1/rules/spells?level={max(levels)}").status_code == 200
    assert client.get(f"/v1/rules/spells?level={min(levels) - 1}").status_code == 422
    assert client.get(f"/v1/rules/spells?level={max(levels) + 1}").status_code == 422


def test_equipment_summary_fields(client: TestClient) -> None:
    items = every_page(client, "/v1/rules/equipment")
    catalog = load_equipment()
    templates = [*catalog.weapons, *catalog.armour, *catalog.gear, *catalog.ammunition]
    for item, template in zip(items, templates, strict=True):
        assert item == {
            "id": template.id,
            "name": template.name,
            "item_type": template.item_type,
            "cost_gp": template.cost_gp,
        }


def test_equipment_item_type_filter(client: TestClient) -> None:
    catalog = load_equipment()
    expected = {
        "weapon": catalog.weapons,
        "armour": catalog.armour,
        "gear": catalog.gear,
        "ammunition": catalog.ammunition,
    }
    for item_type, templates in expected.items():
        items = every_page(client, f"/v1/rules/equipment?item_type={item_type}")
        assert [item["id"] for item in items] == [t.id for t in templates]
        assert {item["item_type"] for item in items} == {item_type}


def test_equipment_item_type_must_be_one_of_the_four(client: TestClient) -> None:
    for value in ("heavy", "no_such_type"):
        response = client.get("/v1/rules/equipment", params={"item_type": value})
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"


def test_equipment_q_and_item_type_combine(client: TestClient) -> None:
    items = every_page(client, "/v1/rules/equipment?q=SWORD&item_type=weapon")
    assert "sword" in [item["id"] for item in items]
    assert {item["item_type"] for item in items} == {"weapon"}


def test_equipment_detail_says_which_kind_it_is(client: TestClient) -> None:
    assert client.get("/v1/rules/equipment/sword").json()["item_type"] == "weapon"
    assert client.get("/v1/rules/equipment/chainmail").json()["item_type"] == "armour"
    assert client.get("/v1/rules/equipment/torch").json()["item_type"] == "gear"
    assert client.get("/v1/rules/equipment/arrows").json()["item_type"] == "ammunition"


def test_magic_item_summary_fields(client: TestClient) -> None:
    items = every_page(client, "/v1/rules/magic-items")
    for item, template in zip(items, load_magic_items().items, strict=True):
        assert item == {"id": template.id, "name": template.name, "category": template.category.value}


def test_magic_item_category_filter(client: TestClient) -> None:
    templates = load_magic_items().items
    for category in {template.category for template in templates}:
        items = every_page(client, f"/v1/rules/magic-items?category={category.value}")
        assert [item["id"] for item in items] == [t.id for t in templates if t.category == category]
    narrowed = every_page(client, "/v1/rules/magic-items?category=potion&q=healing")
    assert "potion_of_healing" in [item["id"] for item in narrowed]


def test_magic_item_category_must_be_a_category(client: TestClient) -> None:
    response = client.get("/v1/rules/magic-items", params={"category": "no_such_category"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_languages(client: TestClient) -> None:
    body = client.get("/v1/rules/languages", params={"limit": 200}).json()
    assert body["items"] == [dumped(language) for language in load_languages().languages]
    assert body["total"] == len(load_languages().languages)
    page = client.get("/v1/rules/languages", params={"limit": 2, "offset": 1}).json()
    assert page["items"] == body["items"][1:3]


def test_tables_list_the_four_names(client: TestClient) -> None:
    body = client.get("/v1/rules/tables").json()
    assert body["items"] == ["abilities", "combat", "encounters", "treasure"]
    assert body["total"] == 4


@pytest.mark.parametrize(
    ("name", "load"),
    [
        ("abilities", load_ability_tables),
        ("combat", load_combat_tables),
        ("encounters", load_encounter_tables),
        ("treasure", load_treasure_tables),
    ],
)
def test_every_listed_table_resolves_to_osrlibs_model(
    client: TestClient, name: str, load: Callable[[], BaseModel]
) -> None:
    assert name in client.get("/v1/rules/tables").json()["items"]
    response = client.get(f"/v1/rules/tables/{name}")
    assert response.status_code == 200
    assert response.json() == dumped(load())


def test_unknown_table_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/rules/tables/no_such_table")
    assert response.status_code == 404
    assert response.json()["resource"] == "table"
    assert response.json()["id"] == "no_such_table"


def command_types() -> list[str]:
    return [cls.model_fields["command_type"].default for cls in ALL_COMMAND_CLASSES]


def test_commands_list_every_osrlib_command(client: TestClient) -> None:
    items = every_page(client, "/v1/rules/commands")
    assert [item["command_type"] for item in items] == command_types()
    for item, cls in zip(items, ALL_COMMAND_CLASSES, strict=True):
        assert item["allowed_modes"] == sorted(mode.value for mode in cls.allowed_modes)
        assert item["kind"] == ("play" if item["command_type"] in PLAY_COMMAND_TYPES else "referee")
        assert item["summary"]
        assert "\n" not in item["summary"]


def test_command_summary_is_the_first_docstring_paragraph(client: TestClient) -> None:
    items = {item["command_type"]: item for item in every_page(client, "/v1/rules/commands")}
    assert items["move_party"]["summary"].startswith("Move the party one cell")
    assert items["move_party"]["allowed_modes"] == ["exploring"]
    assert items["move_party"]["kind"] == "play"
    assert items["grant_item"]["kind"] == "referee"
    assert items["grant_item"]["summary"].startswith("Referee:")


def test_command_summary_reduces_documentation_links_to_their_text(client: TestClient) -> None:
    items = {item["command_type"]: item for item in every_page(client, "/v1/rules/commands")}
    assert "`GrantCoins`" in items["remove_coins"]["summary"]
    assert "[" not in items["remove_coins"]["summary"]
    assert [item["command_type"] for item in items.values() if "[" in item["summary"] or "](" in item["summary"]] == []


def test_commands_kind_filter(client: TestClient) -> None:
    play = every_page(client, "/v1/rules/commands?kind=play")
    referee = every_page(client, "/v1/rules/commands?kind=referee")
    assert {item["kind"] for item in play} == {"play"}
    assert {item["kind"] for item in referee} == {"referee"}
    assert len(play) + len(referee) == len(ALL_COMMAND_CLASSES)


def test_commands_mode_filter(client: TestClient) -> None:
    for mode in SessionMode:
        items = every_page(client, f"/v1/rules/commands?mode={mode.value}")
        expected = [
            cls.model_fields["command_type"].default for cls in ALL_COMMAND_CLASSES if mode in cls.allowed_modes
        ]
        assert [item["command_type"] for item in items] == expected
        assert all(mode.value in item["allowed_modes"] for item in items)


def test_commands_kind_and_mode_combine(client: TestClient) -> None:
    items = every_page(client, "/v1/rules/commands?kind=play&mode=battle")
    assert items
    assert {item["kind"] for item in items} == {"play"}
    assert all("battle" in item["allowed_modes"] for item in items)


def test_commands_reject_an_invalid_kind_or_mode(client: TestClient) -> None:
    for query in ("kind=wizard", "mode=sleeping"):
        response = client.get(f"/v1/rules/commands?{query}")
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"


def test_commands_paging(client: TestClient) -> None:
    body = client.get("/v1/rules/commands", params={"limit": 5, "offset": 3}).json()
    assert body["total"] == len(ALL_COMMAND_CLASSES)
    assert [item["command_type"] for item in body["items"]] == command_types()[3:8]


def test_every_listed_command_resolves_with_its_schema(client: TestClient) -> None:
    for item, cls in zip(every_page(client, "/v1/rules/commands"), ALL_COMMAND_CLASSES, strict=True):
        response = client.get(f"/v1/rules/commands/{item['command_type']}")
        assert response.status_code == 200, item["command_type"]
        body = response.json()
        assert {key: body[key] for key in item} == item
        assert body["json_schema"] == cls.model_json_schema()


def test_unknown_command_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/rules/commands/no_such_command")
    assert response.status_code == 404
    assert response.json()["resource"] == "command"


def test_license_is_the_text_osrlib_ships(client: TestClient) -> None:
    body = client.get("/v1/rules/license").json()
    text = resources.files("osrlib.data").joinpath("LICENSE-OGL.md").read_text(encoding="utf-8")
    assert body == {"name": body["name"], "text": text}
    assert "Open Game License" in body["name"]
    assert "Section 15" in body["text"] or "SECTION 15" in body["text"]


RULES_PATHS = [
    "/v1/rules/classes",
    "/v1/rules/classes/fighter",
    "/v1/rules/monsters",
    "/v1/rules/monsters/goblin",
    "/v1/rules/spells",
    "/v1/rules/spells/magic_missile",
    "/v1/rules/equipment",
    "/v1/rules/equipment/sword",
    "/v1/rules/magic-items",
    "/v1/rules/magic-items/potion_of_healing",
    "/v1/rules/languages",
    "/v1/rules/tables",
    "/v1/rules/tables/abilities",
    "/v1/rules/commands",
    "/v1/rules/commands/move_party",
    "/v1/rules/license",
]


@pytest.mark.parametrize("path", RULES_PATHS)
def test_every_rules_response_is_cacheable_by_the_client(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == CACHE_CONTROL


@pytest.mark.parametrize("path", RULES_PATHS)
def test_a_player_key_reads_the_rules(player: TestClient, path: str) -> None:
    assert player.get(path).status_code == 200


@pytest.mark.parametrize("path", RULES_PATHS)
def test_a_missing_key_is_unauthorized_when_keys_are_configured(player: TestClient, path: str) -> None:
    response = player.get(path, headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert "cache-control" not in response.headers


def test_openapi_describes_each_route_and_the_osrlib_models(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    rules_paths = [path for path in paths if path.startswith("/v1/rules")]
    assert len(rules_paths) == 16
    for path in rules_paths:
        operation = paths[path]["get"]
        assert operation["summary"]
        assert operation["description"]
    monster = paths["/v1/rules/monsters/{monster_id}"]["get"]["responses"]["200"]["content"]["application/json"]
    assert monster["schema"]["$ref"].endswith("/MonsterTemplate")
    assert {"MonsterTemplate", "ClassDefinition", "SpellTemplate", "MagicItemTemplate"} <= set(
        schema["components"]["schemas"]
    )


def test_an_error_response_has_no_cache_header(client: TestClient) -> None:
    for path in ("/v1/rules/monsters/no_such_id", "/v1/rules/commands?kind=wizard"):
        response = client.get(path)
        assert response.status_code in (404, 422)
        assert "cache-control" not in response.headers


def test_every_detail_route_documents_a_404(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    detail_paths = [path for path in paths if path.startswith("/v1/rules") and path.endswith("}")]
    assert len(detail_paths) == 7
    for path in detail_paths:
        assert "404" in paths[path]["get"]["responses"], path
    assert "404" in paths["/v1/rules/spells"]["get"]["responses"]


def test_operation_ids_are_the_function_names(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    operation_ids = [
        operation["get"]["operationId"] for path, operation in paths.items() if path.startswith("/v1/rules")
    ]
    assert sorted(operation_ids) == sorted(
        [
            "list_classes",
            "get_class",
            "list_monsters",
            "get_monster",
            "list_spells",
            "get_spell",
            "list_equipment",
            "get_equipment_item",
            "list_magic_items",
            "get_magic_item",
            "list_languages",
            "list_tables",
            "get_table",
            "list_commands",
            "get_command",
            "get_license",
        ]
    )


def test_every_property_of_the_rules_schemas_has_a_description(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    names = [
        "ClassSummary",
        "MonsterSummary",
        "SpellSummary",
        "EquipmentSummary",
        "MagicItemSummary",
        "CommandSummary",
        "CommandDetail",
        "License",
    ]
    for name in names:
        for prop, definition in schemas[name]["properties"].items():
            assert definition.get("description"), f"{name}.{prop}"


def test_every_path_parameter_has_a_description(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path, operation in paths.items():
        if not path.startswith("/v1/rules"):
            continue
        for parameter in operation["get"].get("parameters", []):
            assert parameter.get("description"), f"{path} {parameter['name']}"


def test_the_two_union_responses_are_named_schemas_that_return_osrlibs_json(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    for path, name in (("/v1/rules/equipment/{item_id}", "EquipmentItem"), ("/v1/rules/tables/{name}", "TableSet")):
        ok = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert ok == {"$ref": f"#/components/schemas/{name}"}
        assert name in schema["components"]["schemas"]
    assert schema["components"]["schemas"]["EquipmentItem"]["discriminator"]["propertyName"] == "item_type"
    assert client.get("/v1/rules/equipment/chainmail").json() == dumped(load_equipment().get("chainmail"))
    assert client.get("/v1/rules/tables/combat").json() == dumped(load_combat_tables())
