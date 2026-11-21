import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from osrlib.core.alignment import Alignment
from osrlib.core.character import CHARACTER_CREATION_STREAM, Character, create_character
from osrlib.core.events import Visibility
from osrlib.core.rng import RngStreams
from osrlib.core.ruleset import Ruleset
from osrlib.crawl.commands import AddJournalEntry, Command, CommandResult, EnterDungeon, MoveParty
from osrlib.crawl.dungeon import Direction
from osrlib.crawl.interpreter import Interpreter
from osrlib.crawl.party import Party
from osrlib.crawl.session import GameSession
from osrlib.persistence import load_game, save_game

from bxapi.adventures import builtin_adventures
from bxapi.db import Database
from bxapi.errors import ApiError
from bxapi.session_manager import MAX_PARTY_SIZE, SessionManager

CLIENT = "game"
BARROW = builtin_adventures()["builtin-barrow"].adventure
ENTER = EnterDungeon(dungeon_id="barrow")
EAST = MoveParty(direction=Direction.EAST)
WEST = MoveParty(direction=Direction.WEST)
NORTH = MoveParty(direction=Direction.NORTH)
ROUTE = (ENTER, EAST, WEST, EAST)


def party() -> Party:
    stream = RngStreams(master_seed=3).get(CHARACTER_CREATION_STREAM)
    members = [
        create_character(name=name, class_id=class_id, alignment=Alignment.LAWFUL, ruleset=Ruleset(), stream=stream)
        for name, class_id in (("Ash", "fighter"), ("Bree", "cleric"))
    ]
    return Party(members=[Character.from_document(made.character.to_document()) for made in members])


def oracle(seed: int) -> GameSession:
    game = GameSession.new(party(), BARROW, seed=seed)
    game.register_listener(Interpreter(game))
    return game


def outcome(result: CommandResult) -> dict[str, Any]:
    return result.model_dump(mode="json")


@pytest.fixture
def path(tmp_path: Path) -> str:
    return str(tmp_path / "sessions.db")


@pytest.fixture
def databases(path: str) -> Iterator[Callable[[], Database]]:
    opened: list[Database] = []

    def _open() -> Database:
        db = Database(path)
        opened.append(db)
        return db

    yield _open
    for db in opened:
        db.close()


def start(manager: SessionManager, seed: int, client: str = CLIENT) -> str:
    row = manager.start(
        client, name=None, adventure_id="builtin-barrow", adventure=BARROW, party=party(), seed=seed, ruleset=None
    )
    return row.id


def test_an_accepted_command_is_in_the_database_before_execute_returns(databases: Callable[[], Database]) -> None:
    db = databases()
    manager = SessionManager(db, cache_size=4)
    session_id = start(manager, 11)
    executed = manager.execute(CLIENT, session_id, ENTER)
    assert executed.result.accepted and executed.revision == 1
    row = db.get_session_with_document(CLIENT, session_id)
    assert row is not None
    assert (row.revision, row.mode) == (1, "exploring")
    expected = oracle(11)
    expected.execute(ENTER)
    assert row.document == save_game(expected)
    assert row.clock_rounds == expected.clock.rounds


def test_a_refused_command_writes_nothing(databases: Callable[[], Database]) -> None:
    db = databases()
    manager = SessionManager(db, cache_size=4)
    session_id = start(manager, 12)
    manager.execute(CLIENT, session_id, ENTER)
    before = db.get_session_with_document(CLIENT, session_id)
    executed = manager.execute(CLIENT, session_id, NORTH)
    assert not executed.result.accepted and executed.revision == 1
    assert db.get_session_with_document(CLIENT, session_id) == before


def test_the_cache_keeps_the_sessions_used_last_and_reloads_the_others(databases: Callable[[], Database]) -> None:
    manager = SessionManager(databases(), cache_size=2)
    first, second, third = start(manager, 1), start(manager, 2), start(manager, 3)
    assert manager.loaded() == [(CLIENT, second), (CLIENT, third)]
    oracles = {first: oracle(1), second: oracle(2), third: oracle(3)}
    for command in ROUTE:
        for session_id in (first, second, third):
            executed = manager.execute(CLIENT, session_id, command)
            assert outcome(executed.result) == outcome(oracles[session_id].execute(command))
            assert len(manager.loaded()) == 2
    for session_id, expected in oracles.items():
        view = manager.read(CLIENT, session_id, lambda game: game.view(Visibility.REFEREE))
        assert view == expected.view(Visibility.REFEREE)


def test_a_session_evicted_while_in_use_finishes_and_queues_the_next_request(
    databases: Callable[[], Database],
) -> None:
    manager = SessionManager(databases(), cache_size=1)
    held = start(manager, 5)
    entered, release = threading.Event(), threading.Event()
    order: list[str] = []

    def slow_read(game: GameSession) -> int:
        entered.set()
        release.wait(5)
        order.append("read")
        return game.clock.rounds

    reader = threading.Thread(target=manager.read, args=(CLIENT, held, slow_read))
    reader.start()
    assert entered.wait(5)
    newest = start(manager, 6)
    assert manager.loaded() == [(CLIENT, newest)], "the cache of one kept the session in use"

    def command() -> None:
        manager.execute(CLIENT, held, ENTER)
        order.append("command")

    writer = threading.Thread(target=command)
    writer.start()
    writer.join(0.2)
    assert writer.is_alive(), "the command ran while the read held the session"
    release.set()
    reader.join(5)
    writer.join(5)
    assert order == ["read", "command"]
    expected = oracle(5)
    expected.execute(ENTER)
    assert manager.read(CLIENT, held, lambda game: save_game(game)) == save_game(expected)


def test_separate_sessions_run_in_parallel(databases: Callable[[], Database]) -> None:
    manager = SessionManager(databases(), cache_size=4)
    busy, free = start(manager, 1), start(manager, 2)
    entered, release = threading.Event(), threading.Event()

    def hold(game: GameSession) -> None:
        entered.set()
        release.wait(5)

    reader = threading.Thread(target=manager.read, args=(CLIENT, busy, hold))
    reader.start()
    assert entered.wait(5)
    try:
        assert manager.execute(CLIENT, free, ENTER).revision == 1
    finally:
        release.set()
        reader.join(5)


def test_threads_on_one_session_execute_one_at_a_time(databases: Callable[[], Database]) -> None:
    db = databases()
    manager = SessionManager(db, cache_size=4)
    session_id = start(manager, 9)
    revisions: list[int] = []

    def work(worker: int) -> None:
        for line in range(5):
            executed = manager.execute(CLIENT, session_id, AddJournalEntry(text=f"{worker}-{line}"))
            revisions.append(executed.revision)

    threads = [threading.Thread(target=work, args=(worker,)) for worker in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(revisions) == list(range(1, 41))
    assert db.get_session_revision(CLIENT, session_id) == 40
    journal = manager.read(CLIENT, session_id, lambda game: [entry.text for entry in game.journal])
    assert sorted(journal) == sorted(f"{worker}-{line}" for worker in range(8) for line in range(5))


def test_a_change_from_another_process_is_loaded_before_use(databases: Callable[[], Database]) -> None:
    here, there = SessionManager(databases(), cache_size=4), SessionManager(databases(), cache_size=4)
    session_id = start(here, 21)
    expected = oracle(21)
    for index, command in enumerate(ROUTE):
        manager = here if index % 2 == 0 else there
        executed = manager.execute(CLIENT, session_id, command)
        assert outcome(executed.result) == outcome(expected.execute(command))
        assert executed.revision == index + 1
    for manager in (here, there):
        assert manager.read(CLIENT, session_id, save_game) == save_game(expected)


class _RacingDatabase(Database):
    """A database whose next session write first lets another process write the same session."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.before_write: Callable[[], object] | None = None

    def update_session(self, client: str, session_id: str, **fields: Any) -> bool:
        hook, self.before_write = self.before_write, None
        if hook is not None:
            hook()
        return super().update_session(client, session_id, **fields)


def test_a_command_that_loses_the_race_leaves_no_trace(path: str, databases: Callable[[], Database]) -> None:
    racing = _RacingDatabase(path)
    try:
        loser = SessionManager(racing, cache_size=4)
        winner = SessionManager(databases(), cache_size=4)
        session_id = start(loser, 31)
        racing.before_write = lambda: winner.execute(CLIENT, session_id, AddJournalEntry(text="winner"))
        with pytest.raises(ApiError) as caught:
            loser.execute(CLIENT, session_id, AddJournalEntry(text="loser"))
        assert caught.value.status == 409 and caught.value.code == "session_busy"
        assert caught.value.extras == {"revision": 1}
        assert loser.loaded() == [], "the losing process kept its copy"
        for manager in (loser, winner):
            journal = manager.read(CLIENT, session_id, lambda game: [entry.text for entry in game.journal])
            assert journal == ["winner"]
        assert racing.get_session_revision(CLIENT, session_id) == 1
        assert loser.execute(CLIENT, session_id, AddJournalEntry(text="again")).revision == 2
    finally:
        racing.close()


def test_a_command_that_raises_is_not_kept(databases: Callable[[], Database]) -> None:
    db = databases()
    manager = SessionManager(db, cache_size=4)
    session_id = start(manager, 41)
    game = manager.read(CLIENT, session_id, lambda game: game)
    original = game.execute

    def apply_then_fail(command: Command) -> CommandResult:
        original(command)
        raise RuntimeError("a listener failed")

    game.execute = apply_then_fail
    with pytest.raises(RuntimeError):
        manager.execute(CLIENT, session_id, ENTER)
    assert db.get_session_revision(CLIENT, session_id) == 0
    assert manager.loaded() == []
    expected = oracle(41)
    executed = manager.execute(CLIENT, session_id, ENTER)
    assert outcome(executed.result) == outcome(expected.execute(ENTER))
    assert executed.revision == 1


def test_expected_revision(databases: Callable[[], Database]) -> None:
    db = databases()
    manager = SessionManager(db, cache_size=4)
    session_id = start(manager, 51)
    manager.execute(CLIENT, session_id, ENTER, expected_revision=0)
    with pytest.raises(ApiError) as caught:
        manager.execute(CLIENT, session_id, EAST, expected_revision=0)
    assert (caught.value.status, caught.value.code, caught.value.extras) == (409, "revision_conflict", {"revision": 1})
    assert db.get_session_revision(CLIENT, session_id) == 1
    assert manager.execute(CLIENT, session_id, EAST, expected_revision=1).revision == 2


def test_a_deleted_session_does_not_come_back_from_the_cache(databases: Callable[[], Database]) -> None:
    db, elsewhere = databases(), databases()
    manager = SessionManager(db, cache_size=4)
    forgotten, deleted_elsewhere = start(manager, 1), start(manager, 2)
    assert db.delete_session(CLIENT, forgotten)
    manager.forget(CLIENT, forgotten)
    assert elsewhere.delete_session(CLIENT, deleted_elsewhere)
    for session_id in (forgotten, deleted_elsewhere):
        with pytest.raises(ApiError) as read:
            manager.read(CLIENT, session_id, save_game)
        with pytest.raises(ApiError) as executed:
            manager.execute(CLIENT, session_id, ENTER)
        for caught in (read, executed):
            assert (caught.value.status, caught.value.extras["id"]) == (404, session_id)
    assert manager.loaded() == []


def test_another_client_gets_404_and_leaves_the_cache_alone(databases: Callable[[], Database]) -> None:
    manager = SessionManager(databases(), cache_size=4)
    session_id = start(manager, 61)
    for call in (
        lambda: manager.read("other", session_id, save_game),
        lambda: manager.execute("other", session_id, ENTER),
    ):
        with pytest.raises(ApiError) as caught:
            call()
        assert caught.value.status == 404
    assert manager.loaded() == [(CLIENT, session_id)]
    assert manager.execute(CLIENT, session_id, ENTER).revision == 1


def test_a_restored_session_plays_on_like_a_loaded_one(databases: Callable[[], Database]) -> None:
    manager = SessionManager(databases(), cache_size=4)
    played = oracle(71)
    played.execute(ENTER)
    document = save_game(played)
    row = manager.restore(CLIENT, name="Again", adventure_id=None, game=load_game(document))
    assert (row.revision, row.name, row.adventure_id, row.mode) == (0, "Again", None, "exploring")
    expected = load_game(document)
    expected.register_listener(Interpreter(expected))
    for command in (EAST, WEST):
        assert outcome(manager.execute(CLIENT, row.id, command).result) == outcome(expected.execute(command))


def test_two_members_with_the_same_id_are_party_invalid(databases: Callable[[], Database]) -> None:
    manager = SessionManager(databases(), cache_size=4)
    member = party().members[0].model_copy(update={"id": "character-0001"})
    twins = Party(members=[member, member.model_copy()])
    with pytest.raises(ApiError) as caught:
        manager.start(
            CLIENT, name=None, adventure_id="builtin-barrow", adventure=BARROW, party=twins, seed=1, ruleset=None
        )
    assert (caught.value.status, caught.value.code) == (422, "party_invalid")
    assert manager.loaded() == []


def test_a_party_larger_than_the_limit_is_party_invalid(databases: Callable[[], Database]) -> None:
    manager = SessionManager(databases(), cache_size=4)
    member = party().members[0]
    crowd = Party(members=[member.model_copy() for _ in range(MAX_PARTY_SIZE + 1)])
    with pytest.raises(ApiError) as caught:
        manager.start(
            CLIENT, name=None, adventure_id="builtin-barrow", adventure=BARROW, party=crowd, seed=1, ruleset=None
        )
    assert (caught.value.status, caught.value.code) == (422, "party_invalid")


def test_a_request_that_waits_too_long_for_the_lock_does_nothing(databases: Callable[[], Database]) -> None:
    db = databases()
    manager = SessionManager(db, cache_size=4, lock_timeout=0.05)
    session_id = start(manager, 81)
    manager.execute(CLIENT, session_id, ENTER)
    before = db.get_session_with_document(CLIENT, session_id)
    entered, release = threading.Event(), threading.Event()

    def hold(_: GameSession) -> None:
        entered.set()
        release.wait(5)

    holder = threading.Thread(target=manager.read, args=(CLIENT, session_id, hold))
    holder.start()
    called: list[str] = []
    try:
        assert entered.wait(5)
        for attempt in (
            lambda: manager.execute(CLIENT, session_id, EAST),
            lambda: manager.read(CLIENT, session_id, lambda game: called.append("read")),
        ):
            with pytest.raises(ApiError) as caught:
                attempt()
            assert (caught.value.status, caught.value.code, caught.value.extras) == (
                409,
                "session_busy",
                {"revision": 1},
            )
    finally:
        release.set()
        holder.join(5)
    assert called == []
    assert db.get_session_with_document(CLIENT, session_id) == before
    expected = oracle(81)
    expected.execute(ENTER)
    assert outcome(manager.execute(CLIENT, session_id, EAST).result) == outcome(expected.execute(EAST))


def test_updated_at_is_the_time_of_the_last_accepted_command(databases: Callable[[], Database]) -> None:
    db = databases()
    times = iter(["2031-01-01T00:00:01Z", "2031-01-01T00:00:02Z"])
    manager = SessionManager(db, cache_size=4, now=lambda: next(times))
    session_id = start(manager, 91)
    created = db.get_session(CLIENT, session_id)
    assert created is not None and created.updated_at == created.created_at
    manager.execute(CLIENT, session_id, ENTER)
    first = db.get_session(CLIENT, session_id)
    assert first is not None and first.updated_at == "2031-01-01T00:00:01Z"
    manager.execute(CLIENT, session_id, NORTH)
    refused = db.get_session(CLIENT, session_id)
    assert refused is not None and refused.updated_at == "2031-01-01T00:00:01Z"
    manager.execute(CLIENT, session_id, EAST)
    second = db.get_session(CLIENT, session_id)
    assert second is not None and second.updated_at == "2031-01-01T00:00:02Z"
    assert second.created_at == created.created_at
