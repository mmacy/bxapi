"""The live sessions: loading them, running one command at a time on each, and writing each change.

[`create_app`][bxapi.app.create_app] makes one [`SessionManager`][bxapi.session_manager.SessionManager]
and puts it at `app.state.sessions`. The session routers call it for everything that runs a game
or reads a running one. Session info, the list of sessions, the commands endpoint, renaming,
deleting, and saving use the database directly, because the `sessions` table has the columns they
need.

The database is the record of every session. The manager keeps a bounded cache of loaded
sessions so that a command doesn't have to load the save document each time. A loaded session is
a copy of what the database has:

- Each session has a lock. A request holds it while it executes a command or reads the session,
  so one session runs one request at a time, and requests for separate sessions run in parallel.
- Before a request uses a loaded copy, the manager compares the copy's revision with the
  revision in the database. When they differ, another server process changed the session, and
  the manager loads it again.
- After the session accepts a command, the manager writes the save document, the mode, the
  clock, and the new revision with a compare-and-swap: the write happens only if the database
  still has the revision the command started from. When it doesn't, another process won the
  race, so the manager throws the loaded copy away and the request gets 409 `session_busy`.
- The manager registers osrlib's `Interpreter` on every session it creates, restores, or loads,
  so the adventure's triggers and quests run, and a loaded session plays on as the original
  would have.
"""

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from osrlib.core.ruleset import Ruleset
from osrlib.crawl.adventure import Adventure
from osrlib.crawl.commands import Command, CommandResult, SessionMode
from osrlib.crawl.interpreter import Interpreter
from osrlib.crawl.party import Party
from osrlib.crawl.session import GameSession
from osrlib.persistence import load_game, save_game

from bxapi.db import Database, SessionRow, utc_now
from bxapi.errors import conflict, invalid, not_found

__all__ = ["UNKNOWN_ADVENTURE_ID", "Executed", "SessionManager"]

UNKNOWN_ADVENTURE_ID = ""
"""The value the `adventure_id` column has for a session whose adventure the server can't identify.

A session restored from an imported save is one. Session info reports the value as `null`."""

type _Key = tuple[str, str]
"""A client name and a session id. Keying by both keeps one client's requests away from another
client's loaded sessions, even for an id the other client sent."""


@dataclass(frozen=True, slots=True)
class Executed:
    """What happened when a session executed a command."""

    result: CommandResult
    """osrlib's result: whether the session accepted the command, and its rejections or events."""
    mode: SessionMode
    """The session's mode after the command."""
    revision: int
    """The session's revision after the command. A refused command leaves it unchanged."""


@dataclass(frozen=True, slots=True)
class _Loaded:
    game: GameSession
    revision: int


class _Slot:
    """The lock of one session, and its loaded copy while it's in the cache.

    A slot lives while the session is in the cache or a request is using it. `users` counts the
    requests that hold the lock or wait for it, so a request that comes in while the session is
    evicted still queues on the same lock.
    """

    __slots__ = ("loaded", "lock", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0
        self.loaded: _Loaded | None = None


class SessionManager:
    """The server's running sessions, each loaded at most once and used by one request at a time.

    Args:
        db: The database that stores the sessions.
        cache_size: The most sessions to keep loaded. When a request loads one more, the manager
            drops the session used longest ago. A request that's using a dropped session finishes
            with its copy, and the next request loads the session from the database again.

    Examples:
        ```python
        from osrlib.core.alignment import Alignment
        from osrlib.core.character import CHARACTER_CREATION_STREAM, create_character
        from osrlib.core.rng import RngStreams
        from osrlib.core.ruleset import Ruleset
        from osrlib.crawl.commands import EnterDungeon
        from osrlib.crawl.party import Party

        from bxapi.adventures import builtin_adventures
        from bxapi.db import Database
        from bxapi.session_manager import SessionManager

        stream = RngStreams(master_seed=1).get(CHARACTER_CREATION_STREAM)
        hero = create_character(
            name="Hild", class_id="fighter", alignment=Alignment.LAWFUL, ruleset=Ruleset(), stream=stream
        ).character
        barrow = builtin_adventures()["builtin-barrow"].adventure

        db = Database(":memory:")
        manager = SessionManager(db, cache_size=8)
        row = manager.start(
            "local",
            name=None,
            adventure_id="builtin-barrow",
            adventure=barrow,
            party=Party(members=[hero]),
            seed=7,
            ruleset=None,
        )
        executed = manager.execute("local", row.id, EnterDungeon(dungeon_id="barrow"))
        assert executed.result.accepted and executed.revision == 1
        assert db.get_session_revision("local", row.id) == 1
        db.close()
        ```
    """

    def __init__(self, db: Database, *, cache_size: int) -> None:
        self._db = db
        self._cache_size = cache_size
        # The mutex guards `_slots`, `_cache`, and each slot's `users` and `loaded`. A request never
        # waits for a session's lock while it holds the mutex, so the two can't deadlock.
        self._mutex = threading.Lock()
        self._slots: dict[_Key, _Slot] = {}
        self._cache: OrderedDict[_Key, _Slot] = OrderedDict()

    # Starting sessions

    def start(
        self,
        client: str,
        *,
        name: str | None,
        adventure_id: str,
        adventure: Adventure,
        party: Party,
        seed: int,
        ruleset: Ruleset | None,
    ) -> SessionRow:
        """Start a new game in town and store it at revision 0.

        Args:
            client: The client the session belongs to.
            name: The session's name, or `None` for the adventure's name.
            adventure_id: The id of the adventure, for session info.
            adventure: The adventure to play.
            party: The party, in marching order. `GameSession.new` gives each member without an
                id the next `character-NNNN` id.
            seed: The master seed.
            ruleset: The optional rules, or `None` for the rules as written.

        Returns:
            The stored session.

        Raises:
            ApiError: With status 422 and the code `party_invalid`, if two members of the party
                end up with the same id. osrlib keeps an id a member already has, so two copies of
                one imported character, or an imported character whose id matches one osrlib
                assigns, would collide. Every command names a member by id.
            osrlib.errors.ContentValidationError: If `GameSession.new` refuses the adventure.
        """
        game = GameSession.new(party, adventure, seed=seed, ruleset=ruleset)
        member_ids = [member.id for member in game.party.members]
        if len(set(member_ids)) != len(member_ids):
            raise invalid("party_invalid", "Two characters in the party have the same id.")
        return self._store_new(client, name=name, adventure_id=adventure_id, game=game)

    def restore(self, client: str, *, name: str | None, adventure_id: str | None, game: GameSession) -> SessionRow:
        """Store a session that osrlib's `load_game` loaded from a save document, as a new session at revision 0.

        Args:
            client: The client the new session belongs to.
            name: The session's name, or `None` for the adventure's name.
            adventure_id: The id of the adventure the save plays, or `None` when the server can't
                tell. The session stores [`UNKNOWN_ADVENTURE_ID`][bxapi.session_manager.UNKNOWN_ADVENTURE_ID]
                for `None`.
            game: The loaded session, with no listeners registered.

        Returns:
            The stored session.
        """
        return self._store_new(client, name=name, adventure_id=adventure_id, game=game)

    def _store_new(self, client: str, *, name: str | None, adventure_id: str | None, game: GameSession) -> SessionRow:
        _attach_listeners(game)
        row = self._db.insert_session(
            client,
            name=name if name is not None else game.adventure.name,
            adventure_id=adventure_id if adventure_id is not None else UNKNOWN_ADVENTURE_ID,
            adventure_name=game.adventure.name,
            mode=game.mode.value,
            clock_rounds=game.clock.rounds,
            document=save_game(game),
        )
        with self._locked((client, row.id)) as slot:
            self._remember((client, row.id), slot, _Loaded(game, row.revision))
        return row

    # Using sessions

    def execute(
        self, client: str, session_id: str, command: Command, *, expected_revision: int | None = None
    ) -> Executed:
        """Execute one command, and write the session to the database if the session accepted it.

        The database has the new state before the method returns. A refused command writes
        nothing and leaves the revision as it was.

        Args:
            client: The caller's client.
            session_id: The session's id.
            command: The command to execute.
            expected_revision: The revision the caller expects the session to have, or `None` to
                skip the check.

        Returns:
            osrlib's result, with the session's mode and revision after the command.

        Raises:
            ApiError: With status 404 and the code `not_found`, if the client has no session with
                that id. With status 409 and the code `revision_conflict`, if `expected_revision`
                isn't the session's revision. With status 409 and the code `session_busy`, if
                another server process wrote the session while this one executed the command.
        """
        key = (client, session_id)
        with self._locked(key) as slot:
            loaded = self._current(key, slot)
            if expected_revision is not None and expected_revision != loaded.revision:
                raise conflict(
                    "revision_conflict",
                    f"The session is at revision {loaded.revision}, not {expected_revision}. Read the view and decide "
                    "whether to send the command again.",
                    revision=loaded.revision,
                )
            game = loaded.game
            try:
                result = game.execute(command)
                if not result.accepted:
                    return Executed(result=result, mode=game.mode, revision=loaded.revision)
                revision = loaded.revision + 1
                written = self._db.update_session(
                    client,
                    session_id,
                    expected_revision=loaded.revision,
                    document=save_game(game),
                    mode=game.mode.value,
                    clock_rounds=game.clock.rounds,
                    revision=revision,
                    updated_at=utc_now(),
                )
            except BaseException:
                # osrlib may have applied part of the command before the exception, and the
                # database doesn't have it, so the loaded copy no longer matches the database.
                self._drop(key, slot)
                raise
            if not written:
                self._drop(key, slot)
                if self._db.get_session_revision(client, session_id) is None:
                    raise not_found("session", session_id)
                raise conflict(
                    "session_busy",
                    "Another server process changed the session while this one executed the command. The command "
                    "had no effect. Send it again.",
                )
            self._remember(key, slot, _Loaded(game, revision))
            return Executed(result=result, mode=game.mode, revision=revision)

    def read[T](self, client: str, session_id: str, reader: Callable[[GameSession], T]) -> T:
        """Call a function on a session while no command can run on it, and return its result.

        Use it to build a view or to read the event log. The function must not change the
        session: the manager doesn't write anything after it returns.

        Args:
            client: The caller's client.
            session_id: The session's id.
            reader: The function to call with the loaded session.

        Returns:
            What `reader` returned.

        Raises:
            ApiError: With status 404 and the code `not_found`, if the client has no session with
                that id.
        """
        key = (client, session_id)
        with self._locked(key) as slot:
            return reader(self._current(key, slot).game)

    def forget(self, client: str, session_id: str) -> None:
        """Drop a session's loaded copy, if the cache has one. Call it after you delete the session.

        Args:
            client: The client the session belonged to.
            session_id: The session's id.
        """
        key = (client, session_id)
        with self._mutex:
            slot = self._cache.pop(key, None)
            if slot is not None:
                slot.loaded = None
                if slot.users == 0:
                    del self._slots[key]

    def loaded(self) -> list[tuple[str, str]]:
        """Return the client and id of each session in the cache, from the one used longest ago to the latest."""
        with self._mutex:
            return list(self._cache)

    # The cache

    @contextmanager
    def _locked(self, key: _Key) -> Iterator[_Slot]:
        with self._mutex:
            slot = self._slots.get(key)
            if slot is None:
                slot = self._slots[key] = _Slot()
            slot.users += 1
        try:
            with slot.lock:
                yield slot
        finally:
            with self._mutex:
                slot.users -= 1
                if slot.users == 0 and slot.loaded is None:
                    del self._slots[key]

    def _current(self, key: _Key, slot: _Slot) -> _Loaded:
        """Return the session as the database has it now. The caller holds the slot's lock."""
        client, session_id = key
        with self._mutex:
            loaded = slot.loaded
        if loaded is not None:
            stored = self._db.get_session_revision(client, session_id)
            if stored == loaded.revision:
                self._remember(key, slot, loaded)
                return loaded
        row = self._db.get_session_with_document(client, session_id)
        if row is None:
            self._drop(key, slot)
            raise not_found("session", session_id)
        game = load_game(row.document)
        _attach_listeners(game)
        loaded = _Loaded(game, row.revision)
        self._remember(key, slot, loaded)
        return loaded

    def _remember(self, key: _Key, slot: _Slot, loaded: _Loaded) -> None:
        """Put a loaded session in the cache as the latest used, and evict the oldest past the limit."""
        with self._mutex:
            slot.loaded = loaded
            self._cache[key] = slot
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                old_key, old = self._cache.popitem(last=False)
                old.loaded = None
                if old.users == 0:
                    del self._slots[old_key]

    def _drop(self, key: _Key, slot: _Slot) -> None:
        """Take a session out of the cache. The caller holds the slot's lock, so the slot stays."""
        with self._mutex:
            slot.loaded = None
            self._cache.pop(key, None)


def _attach_listeners(game: GameSession) -> None:
    # A save contains data and no code, so every session the server builds needs its listener again.
    game.register_listener(Interpreter(game))
