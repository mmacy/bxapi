import copy

import pytest
from osrlib.core.alignment import Alignment
from osrlib.core.character import CHARACTER_CREATION_STREAM, create_character
from osrlib.core.rng import RngStreams
from osrlib.core.ruleset import Ruleset
from osrlib.crawl.adventure import Adventure
from osrlib.crawl.commands import EnterDungeon, SessionMode
from osrlib.crawl.interpreter import Interpreter
from osrlib.crawl.party import Party
from osrlib.crawl.session import GameSession
from osrlib.errors import ContentValidationError, SaveVersionError
from osrlib.versioning import SCHEMA_VERSION

from bxapi.adventures import (
    ADVENTURE_KIND,
    BUILTIN_PREFIX,
    adventure_document,
    builtin_adventures,
    find_adventure,
    is_builtin,
    parse_adventure,
)
from bxapi.db import Database
from bxapi.errors import ApiError


def _barrow_document() -> dict:
    return copy.deepcopy(dict(builtin_adventures()["builtin-barrow"].document))


def test_every_builtin_parses_and_has_a_prefixed_id() -> None:
    adventures = builtin_adventures()
    assert "builtin-barrow" in adventures
    assert list(adventures) == sorted(adventures)
    for adventure_id, builtin in adventures.items():
        assert is_builtin(adventure_id)
        assert adventure_id.startswith(BUILTIN_PREFIX)
        assert builtin.id == adventure_id
        assert builtin.document["kind"] == ADVENTURE_KIND
        assert isinstance(builtin.adventure, Adventure)


def test_builtins_are_loaded_once_and_read_only() -> None:
    assert builtin_adventures() is builtin_adventures()
    with pytest.raises(TypeError):
        builtin_adventures()["builtin-x"] = builtin_adventures()["builtin-barrow"]  # pyright: ignore[reportIndexIssue]


def test_a_document_round_trips() -> None:
    barrow = builtin_adventures()["builtin-barrow"].adventure
    document = adventure_document(barrow)
    assert document["kind"] == ADVENTURE_KIND
    assert document["schema_version"] == SCHEMA_VERSION
    assert parse_adventure(document) == barrow


def test_the_barrow_starts_a_session() -> None:
    barrow = builtin_adventures()["builtin-barrow"].adventure
    stream = RngStreams(master_seed=1).get(CHARACTER_CREATION_STREAM)
    hero = create_character(
        name="Hild", class_id="fighter", alignment=Alignment.LAWFUL, ruleset=Ruleset(), stream=stream
    ).character
    session = GameSession.new(Party(members=[hero]), barrow, seed=1)
    session.register_listener(Interpreter(session))
    assert session.execute(EnterDungeon(dungeon_id=barrow.dungeons[0].id)).accepted
    assert session.mode is SessionMode.EXPLORING


def test_another_kind_of_document_is_refused() -> None:
    document = _barrow_document() | {"kind": "save"}
    with pytest.raises(ContentValidationError):
        parse_adventure(document)


def test_a_newer_schema_version_is_refused() -> None:
    document = _barrow_document() | {"schema_version": SCHEMA_VERSION + 1}
    with pytest.raises(SaveVersionError):
        parse_adventure(document)


def test_a_malformed_payload_lists_each_problem() -> None:
    document = _barrow_document()
    del document["payload"]["town"]
    document["payload"]["dungeons"] = []
    with pytest.raises(ContentValidationError) as refused:
        parse_adventure(document)
    assert "town" in str(refused.value)
    assert "dungeons" in str(refused.value)


def test_a_dangling_reference_is_refused() -> None:
    document = _barrow_document()
    document["payload"]["town"]["travel_turns"] = {"no-such-dungeon": 1}
    with pytest.raises(ContentValidationError):
        parse_adventure(document)


def test_is_builtin_reads_only_the_prefix() -> None:
    assert is_builtin("builtin-anything")
    assert not is_builtin("adv_abc")


def test_find_adventure_reads_builtins_and_the_clients_adventures() -> None:
    db = Database(":memory:")
    barrow = builtin_adventures()["builtin-barrow"].adventure
    stored = db.insert_adventure("game", name=barrow.name, info={}, document=_barrow_document())
    assert find_adventure(db, "game", "builtin-barrow") is barrow
    assert find_adventure(db, "other", "builtin-barrow") is barrow
    assert find_adventure(db, "game", stored.id) == barrow
    for client, adventure_id in [("other", stored.id), ("game", "adv_missing"), ("game", "builtin-missing")]:
        with pytest.raises(ApiError) as refused:
            find_adventure(db, client, adventure_id)
        assert (refused.value.status, refused.value.code) == (404, "not_found")
        assert refused.value.extras == {"resource": "adventure", "id": adventure_id}
    db.close()
