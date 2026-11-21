import multiprocessing
import re
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bxapi.app import create_app
from bxapi.db import (
    LAYOUT_VERSION,
    SQLITE_INTEGER_MAX,
    SQLITE_INTEGER_MIN,
    Database,
    DatabaseOpenError,
    fits_integer_column,
    utc_now,
)
from bxapi.settings import Settings

from .conftest import LOCAL_URL

ME = "game"
OTHER = "other"


def _document(n: int = 0, *, schema_version: int = 4, engine_version: str = "2.4.0") -> dict[str, Any]:
    return {"kind": "save", "schema_version": schema_version, "engine_version": engine_version, "payload": {"n": n}}


def _session(db: Database, client: str = ME, *, mode: str = "town", name: str = "Run") -> str:
    row = db.insert_session(
        client,
        name=name,
        adventure_id="builtin-keep",
        adventure_name="The Keep",
        mode=mode,
        clock_rounds=0,
        document=_document(),
    )
    return row.id


def _advance(db: Database, client: str, session_id: str, expected: int) -> bool:
    return db.update_session(
        client,
        session_id,
        expected_revision=expected,
        document=_document(expected + 1),
        mode="dungeon",
        clock_rounds=10 * (expected + 1),
        revision=expected + 1,
        updated_at=utc_now(),
    )


_start: Any = None


def _set_start(barrier: Any) -> None:
    global _start
    _start = barrier


def _increment_until_done(path: str, session_id: str, attempts: int) -> int:
    """Read the revision and try to advance it, `attempts` times. Return how many writes won."""
    db = Database(path)
    won = 0
    _start.wait()
    for _ in range(attempts):
        revision = db.get_session_revision(ME, session_id)
        assert revision is not None
        won += _advance(db, ME, session_id, revision)
    db.close()
    return won


def _open_and_close(path: str) -> str:
    """Wait for the other processes, open a database on `path`, and return what went wrong, if anything."""
    _start.wait()
    try:
        Database(path).close()
    except Exception as error:
        return repr(error)
    return ""


@pytest.fixture(params=["memory", "file"])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Database]:
    database = Database(":memory:" if request.param == "memory" else str(tmp_path / "bxapi.db"))
    yield database
    database.close()


@pytest.fixture
def path(tmp_path: Path) -> str:
    return str(tmp_path / "shared.db")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Set the time that `utc_now` returns in `bxapi.db`."""

    def _set(stamp: str) -> None:
        monkeypatch.setattr("bxapi.db.utc_now", lambda: stamp)

    return _set


def test_utc_now_is_rfc3339_with_whole_seconds() -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", utc_now())


CHARACTER = {"name": "Hild", "class_id": "fighter", "level": 1, "max_hp": 8, "document": {"kind": "character"}}


def test_ability_roll_round_trip_and_client_filter(db: Database) -> None:
    roll = db.insert_ability_roll(ME, scores={"str": 9, "dex": 14}, seed=2**53 - 1)
    assert roll.id.startswith("rol_")
    assert db.get_ability_roll(ME, roll.id) == roll
    assert db.get_ability_roll(OTHER, roll.id) is None
    assert db.get_ability_roll(ME, "rol_missing") is None


def test_a_character_from_a_roll_marks_the_roll_used_once(db: Database) -> None:
    roll = db.insert_ability_roll(ME, scores={"str": 9, "dex": 14}, seed=1)

    assert db.insert_character_from_ability_roll(OTHER, roll.id, **CHARACTER) is None
    assert db.get_ability_roll(ME, roll.id) == roll
    assert db.list_characters(OTHER, limit=10, offset=0).total == 0

    row = db.insert_character_from_ability_roll(ME, roll.id, **CHARACTER)
    assert row is not None and row.id.startswith("chr_")
    assert db.get_character(ME, row.id) == row
    stored = db.get_ability_roll(ME, roll.id)
    assert stored is not None and stored.used
    assert stored.scores == {"str": 9, "dex": 14}

    assert db.insert_character_from_ability_roll(ME, roll.id, **CHARACTER) is None
    assert db.insert_character_from_ability_roll(ME, "rol_missing", **CHARACTER) is None
    assert db.list_characters(ME, limit=10, offset=0).total == 1


def test_a_failed_insert_leaves_the_roll_unused(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    roll = db.insert_ability_roll(ME, scores={}, seed=1)
    existing = db.insert_character(ME, **CHARACTER)
    # A repeated id breaks the UNIQUE constraint after the roll is marked, inside the transaction.
    monkeypatch.setattr("bxapi.db.new_id", lambda prefix: existing.id)
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_character_from_ability_roll(ME, roll.id, **CHARACTER)
    stored = db.get_ability_roll(ME, roll.id)
    assert stored is not None and not stored.used
    assert db.list_characters(ME, limit=10, offset=0).total == 1


def test_one_thread_creates_a_character_from_a_roll(db: Database) -> None:
    roll = db.insert_ability_roll(ME, scores={}, seed=1)
    threads = 32
    barrier = threading.Barrier(threads)

    def create() -> bool:
        barrier.wait()
        return db.insert_character_from_ability_roll(ME, roll.id, **CHARACTER) is not None

    with ThreadPoolExecutor(threads) as pool:
        results = list(pool.map(lambda _: create(), range(threads)))
    assert results.count(True) == 1
    assert db.list_characters(ME, limit=50, offset=0).total == 1


def test_one_thread_creates_a_character_from_a_roll_across_two_databases(path: str) -> None:
    first, second = Database(path), Database(path)
    roll = first.insert_ability_roll(ME, scores={}, seed=1)
    threads = 32
    barrier = threading.Barrier(threads)

    def create(index: int) -> bool:
        barrier.wait()
        database = first if index % 2 else second
        return database.insert_character_from_ability_roll(ME, roll.id, **CHARACTER) is not None

    with ThreadPoolExecutor(threads) as pool:
        results = list(pool.map(create, range(threads)))
    assert results.count(True) == 1
    assert first.list_characters(ME, limit=50, offset=0).total == 1
    first.close()
    second.close()


def test_character_round_trip_and_client_filter(db: Database) -> None:
    document = {"kind": "character", "payload": {"name": "Brother Aldric"}}
    row = db.insert_character(ME, name="Brother Aldric", class_id="cleric", level=1, max_hp=5, document=document)
    assert row.id.startswith("chr_")
    assert db.get_character(ME, row.id) == row
    assert db.get_character(OTHER, row.id) is None
    assert db.list_characters(OTHER, limit=50, offset=0).total == 0

    summary = db.list_characters(ME, limit=50, offset=0)
    assert summary.total == 1
    assert summary.items[0].name == "Brother Aldric"
    assert not hasattr(summary.items[0], "document")

    assert not db.delete_character(OTHER, row.id)
    assert db.get_character(ME, row.id) is not None
    assert db.delete_character(ME, row.id)
    assert db.get_character(ME, row.id) is None
    assert not db.delete_character(ME, row.id)


def test_adventure_round_trip_and_client_filter(db: Database) -> None:
    info = {"town": "Threshold", "dungeons": [{"id": "d1", "levels": 2}]}
    document = {"kind": "adventure", "payload": {"secret": "the map"}}
    row = db.insert_adventure(ME, name="The Cold Vein", info=info, document=document)
    assert row.id.startswith("adv_")
    assert db.get_adventure(ME, row.id) == row
    assert row.info == info
    assert db.get_adventure_document(ME, row.id) == document

    assert db.get_adventure(OTHER, row.id) is None
    assert db.get_adventure_document(OTHER, row.id) is None
    assert db.list_adventures(OTHER, limit=50, offset=0).total == 0
    assert db.list_adventures(ME, limit=50, offset=0).items == [row]

    assert not db.delete_adventure(OTHER, row.id)
    assert db.delete_adventure(ME, row.id)
    assert db.get_adventure(ME, row.id) is None
    assert db.get_adventure_document(ME, row.id) is None


def test_session_round_trip_and_client_filter(db: Database) -> None:
    session_id = _session(db)
    row = db.get_session(ME, session_id)
    assert row is not None
    assert row.id.startswith("ses_")
    assert (row.revision, row.mode, row.schema_version, row.engine_version) == (0, "town", 4, "2.4.0")
    assert row.created_at == row.updated_at
    assert not hasattr(row, "document")

    full = db.get_session_with_document(ME, session_id)
    assert full is not None and full.document == _document()
    assert db.get_session_revision(ME, session_id) == 0

    assert db.get_session(OTHER, session_id) is None
    assert db.get_session_with_document(OTHER, session_id) is None
    assert db.get_session_revision(OTHER, session_id) is None
    assert db.list_sessions(OTHER, limit=50, offset=0).total == 0
    assert db.list_sessions(ME, limit=50, offset=0).items == [row]


def test_rename_session_filters_on_client(db: Database) -> None:
    session_id = _session(db)
    assert not db.rename_session(OTHER, session_id, "Stolen")
    assert db.rename_session(ME, session_id, "Second try")
    row = db.get_session(ME, session_id)
    assert row is not None
    assert (row.name, row.revision) == ("Second try", 0)
    assert not db.rename_session(ME, "ses_missing", "Nobody")


def test_update_session_writes_only_at_the_expected_revision(db: Database, clock: Callable[[str], None]) -> None:
    clock("2026-11-20T10:00:00Z")
    session_id = _session(db)
    newer = _document(1, schema_version=5, engine_version="2.5.0")

    def write(client: str, expected: int) -> bool:
        return db.update_session(
            client,
            session_id,
            expected_revision=expected,
            document=newer,
            mode="dungeon",
            clock_rounds=12,
            revision=expected + 1,
            updated_at="2026-11-20T10:05:00Z",
        )

    assert not write(OTHER, 0)
    assert not write(ME, 3)
    unchanged = db.get_session_with_document(ME, session_id)
    assert unchanged is not None and (unchanged.revision, unchanged.document) == (0, _document())

    assert write(ME, 0)
    row = db.get_session_with_document(ME, session_id)
    assert row is not None
    assert (row.revision, row.mode, row.clock_rounds) == (1, "dungeon", 12)
    assert (row.schema_version, row.engine_version) == (5, "2.5.0")
    assert (row.created_at, row.updated_at) == ("2026-11-20T10:00:00Z", "2026-11-20T10:05:00Z")
    assert row.document == newer

    assert not write(ME, 0)
    assert db.get_session_revision(ME, session_id) == 1


def test_update_session_from_two_databases_on_one_file(path: str) -> None:
    first, second = Database(path), Database(path)
    session_id = _session(first)
    assert first.get_session_revision(ME, session_id) == 0
    assert second.get_session_revision(ME, session_id) == 0

    assert _advance(first, ME, session_id, 0)
    assert not _advance(second, ME, session_id, 0)
    assert second.get_session_revision(ME, session_id) == 1
    assert _advance(second, ME, session_id, 1)
    assert first.get_session_revision(ME, session_id) == 2
    first.close()
    second.close()


def test_one_writer_wins_each_revision_across_threads_and_databases(path: str) -> None:
    first, second = Database(path), Database(path)
    session_id = _session(first)
    threads = 16
    barrier = threading.Barrier(threads)

    def write(index: int) -> bool:
        barrier.wait()
        return _advance(first if index % 2 else second, ME, session_id, 0)

    with ThreadPoolExecutor(threads) as pool:
        results = list(pool.map(write, range(threads)))
    assert results.count(True) == 1
    assert second.get_session_revision(ME, session_id) == 1
    first.close()
    second.close()


def test_no_write_is_lost_across_processes(path: str) -> None:
    db = Database(path)
    session_id = _session(db)
    processes, attempts = 4, 300
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(processes)
    with ProcessPoolExecutor(processes, mp_context=context, initializer=_set_start, initargs=(start,)) as pool:
        futures = [pool.submit(_increment_until_done, path, session_id, attempts) for _ in range(processes)]
        won = sum(future.result() for future in futures)
    row = db.get_session_with_document(ME, session_id)
    assert row is not None
    assert 0 < won < processes * attempts
    assert row.revision == won
    assert row.document == _document(won)
    db.close()


def test_delete_session_filters_on_client_and_leaves_saves(db: Database) -> None:
    session_id = _session(db)
    save = db.insert_save(
        ME,
        session_id=session_id,
        adventure_id="builtin-keep",
        adventure_name="The Keep",
        label=None,
        mode="town",
        clock_rounds=0,
        document={},
    )
    assert not db.delete_session(OTHER, session_id)
    assert db.get_session(ME, session_id) is not None
    assert db.delete_session(ME, session_id)
    assert db.get_session(ME, session_id) is None
    assert not db.delete_session(ME, session_id)
    assert db.get_save(ME, save.id) == save


def test_save_round_trip_and_client_filter(db: Database) -> None:
    document = _document(7)
    row = db.insert_save(
        ME,
        session_id="ses_gone",
        adventure_id="adv_gone",
        adventure_name="The Keep",
        label="Before the stairs",
        mode="dungeon",
        clock_rounds=30,
        document=document,
    )
    assert row.id.startswith("sav_")
    assert db.get_save(ME, row.id) == row
    assert db.get_save_document(ME, row.id) == document
    assert row.adventure_id == "adv_gone"
    imported = db.insert_save(
        ME,
        session_id=None,
        adventure_id=None,
        adventure_name="The Keep",
        label=None,
        mode="town",
        clock_rounds=0,
        document=document,
    )
    assert db.get_save(ME, imported.id) == imported
    assert imported.adventure_id is None

    assert db.get_save(OTHER, row.id) is None
    assert db.get_save_document(OTHER, row.id) is None
    assert db.list_saves(OTHER, limit=50, offset=0).total == 0

    assert not db.update_save_label(OTHER, row.id, "Mine now")
    assert db.update_save_label(ME, row.id, "At the stairs")
    stored = db.get_save(ME, row.id)
    assert stored is not None and stored.label == "At the stairs"
    assert db.update_save_label(ME, row.id, None)
    stored = db.get_save(ME, row.id)
    assert stored is not None and stored.label is None

    assert not db.delete_save(OTHER, row.id)
    assert db.delete_save(ME, row.id)
    assert db.get_save(ME, row.id) is None
    assert db.get_save_document(ME, row.id) is None
    assert not db.delete_save(ME, row.id)


def test_pages_are_newest_first_with_totals(db: Database) -> None:
    ids = [
        db.insert_character(ME, name=f"c{n}", class_id="fighter", level=1, max_hp=8, document={}).id for n in range(5)
    ]
    db.insert_character(OTHER, name="theirs", class_id="thief", level=1, max_hp=4, document={})

    pages = [db.list_characters(ME, limit=2, offset=offset) for offset in (0, 2, 4, 6)]
    assert [page.total for page in pages] == [5, 5, 5, 5]
    assert [[row.id for row in page.items] for page in pages] == [ids[4:2:-1], ids[2:0:-1], ids[0:1], []]


def test_rows_with_the_same_second_are_in_reverse_insert_order(db: Database, clock: Callable[[str], None]) -> None:
    clock("2026-11-20T12:00:00Z")
    ids = [_session(db, name=f"s{n}") for n in range(4)]
    listed = [row.id for row in db.list_sessions(ME, limit=50, offset=0).items]
    assert listed == ids[::-1]
    assert [row.id for row in db.list_sessions(ME, limit=50, offset=0).items] == listed


def test_created_at_orders_before_insert_order(db: Database, clock: Callable[[str], None]) -> None:
    clock("2026-11-20T12:00:05Z")
    later = db.insert_adventure(ME, name="later", info={}, document={})
    clock("2026-11-20T12:00:01Z")
    earlier = db.insert_adventure(ME, name="earlier", info={}, document={})
    assert db.list_adventures(ME, limit=50, offset=0).items == [later, earlier]


def test_session_pages_filter_on_mode(db: Database) -> None:
    town = [_session(db, mode="town") for _ in range(3)]
    dungeon = [_session(db, mode="dungeon") for _ in range(2)]
    _session(db, OTHER, mode="town")

    page = db.list_sessions(ME, limit=2, offset=0, mode="town")
    assert page.total == 3
    assert [row.id for row in page.items] == town[:0:-1]
    assert [row.id for row in db.list_sessions(ME, limit=50, offset=0, mode="dungeon").items] == dungeon[::-1]
    assert db.list_sessions(ME, limit=50, offset=0).total == 5
    assert db.list_sessions(ME, limit=50, offset=0, mode="victory").total == 0


def test_save_pages_filter_on_session(db: Database) -> None:
    def save(client: str, session_id: str | None) -> str:
        return db.insert_save(
            client,
            session_id=session_id,
            adventure_id=None,
            adventure_name="a",
            label=None,
            mode="town",
            clock_rounds=0,
            document={},
        ).id

    first = [save(ME, "ses_a") for _ in range(3)]
    save(ME, "ses_b")
    save(ME, None)
    save(OTHER, "ses_a")

    page = db.list_saves(ME, limit=2, offset=1, session_id="ses_a")
    assert page.total == 3
    assert [row.id for row in page.items] == first[1::-1]
    assert db.list_saves(ME, limit=50, offset=0).total == 5


def test_memory_database_is_shared_across_threads() -> None:
    db = Database(":memory:")
    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(lambda _: db.insert_ability_roll(ME, scores={}, seed=1).id, range(40)))
    assert all(db.get_ability_roll(ME, roll_id) is not None for roll_id in ids)

    roll = db.insert_ability_roll(ME, scores={"strength": 3}, seed=1)
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(db.get_ability_roll, ME, roll.id).result() == roll
    db.close()


def test_two_databases_on_one_file_see_each_other(path: str) -> None:
    first, second = Database(path), Database(path)
    row = first.insert_character(ME, name="Shared", class_id="elf", level=1, max_hp=6, document={"n": 1})
    assert second.get_character(ME, row.id) == row
    assert second.delete_character(ME, row.id)
    assert first.get_character(ME, row.id) is None
    first.close()
    second.close()


def test_reopening_a_file_keeps_its_rows(path: str) -> None:
    db = Database(path)
    session_id = _session(db)
    db.close()
    reopened = Database(path)
    assert reopened.get_session_revision(ME, session_id) == 0
    reopened.close()


def test_closed_database_refuses_queries() -> None:
    db = Database(":memory:")
    db.close()
    with pytest.raises(sqlite3.ProgrammingError):
        db.get_session(ME, "ses_x")


def test_app_opens_the_database_and_closes_it_at_shutdown(settings: Settings) -> None:
    app = create_app(settings)
    db = app.state.db
    assert isinstance(db, Database)
    session_id = _session(db)
    with TestClient(app, base_url=LOCAL_URL) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/meta").status_code == 200
        assert db.get_session_revision(ME, session_id) == 0
    with pytest.raises(sqlite3.ProgrammingError):
        db.get_session(ME, session_id)


def test_app_database_works_without_entering_the_client(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"database": ":memory:"}))
    TestClient(app).get("/health")
    assert app.state.db.insert_ability_roll(ME, scores={}, seed=1).id.startswith("rol_")
    app.state.db.close()


def test_a_new_file_gets_the_layout_version(path: str) -> None:
    Database(path).close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LAYOUT_VERSION


def test_a_file_with_another_layout_version_is_refused(path: str) -> None:
    Database(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {LAYOUT_VERSION + 1}")
    with pytest.raises(DatabaseOpenError) as refused:
        Database(path)
    message = str(refused.value)
    assert path in message
    assert f"version {LAYOUT_VERSION + 1}" in message and f"version {LAYOUT_VERSION}." in message


def test_a_file_with_tables_and_no_version_is_refused(path: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE notes (body TEXT)")
    with pytest.raises(DatabaseOpenError, match="version 0"):
        Database(path)


def test_an_empty_file_gets_the_tables(path: str) -> None:
    Path(path).touch()
    db = Database(path)
    assert db.get_session_revision(ME, _session(db)) == 0
    db.close()


@pytest.mark.parametrize("make", ["missing directory", "not a database"])
def test_an_unusable_path_is_refused_with_the_path(tmp_path: Path, make: str) -> None:
    if make == "missing directory":
        target = tmp_path / "missing" / "bxapi.db"
    else:
        target = tmp_path / "notes.txt"
        target.write_text("These are not the tables you are looking for. " * 100)
    with pytest.raises(DatabaseOpenError) as refused:
        Database(str(target))
    assert str(target) in str(refused.value)


def test_the_database_files_are_private(path: str) -> None:
    db = Database(path)
    _session(db)
    for suffix in ("", "-wal", "-shm"):
        mode = stat.S_IMODE(Path(path + suffix).stat().st_mode)
        assert mode == 0o600, f"{path + suffix} has mode {mode:o}"
    db.close()


def test_processes_that_open_a_new_file_together_all_succeed(tmp_path: Path) -> None:
    processes, rounds = 4, 30
    context = multiprocessing.get_context("spawn")
    start = context.Barrier(processes)
    with ProcessPoolExecutor(processes, mp_context=context, initializer=_set_start, initargs=(start,)) as pool:
        for round_number in range(rounds):
            path = str(tmp_path / f"new-{round_number}.db")
            failures = [problem for problem in pool.map(_open_and_close, [path] * processes) if problem]
            assert not failures, f"round {round_number}: {failures[0]}"
            db = Database(path)
            assert db.get_session_revision(ME, _session(db)) == 0
            db.close()


def test_a_session_with_no_adventure_id_stores_null(db: Database) -> None:
    row = db.insert_session(
        ME,
        name="Imported",
        adventure_id=None,
        adventure_name="The Keep",
        mode="town",
        clock_rounds=0,
        document=_document(0),
    )
    assert row.adventure_id is None
    assert db.get_session(ME, row.id) == row


def test_the_integer_range_of_a_column() -> None:
    assert fits_integer_column(SQLITE_INTEGER_MAX) and fits_integer_column(SQLITE_INTEGER_MIN)
    assert not fits_integer_column(SQLITE_INTEGER_MAX + 1)
    assert not fits_integer_column(SQLITE_INTEGER_MIN - 1)
