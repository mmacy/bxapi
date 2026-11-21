"""The SQLite database, and the only code in bxapi that runs SQL.

[`create_app`][bxapi.app.create_app] opens one [`Database`][bxapi.db.Database] for the file in
the settings and puts it at `app.state.db`. An endpoint reads it from `request.app.state.db`.

Every method that reads, changes, or deletes a row takes the caller's client name and matches
only that client's rows. A row that belongs to another client looks the same as a row that
doesn't exist: a read returns `None`, and an update or a delete returns `False`.

A JSON column goes in as a Python object and comes out as one. The database stores it as text.
"""

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

from bxapi.ids import new_id

__all__ = [
    "BUSY_TIMEOUT_SECONDS",
    "LAYOUT_VERSION",
    "AbilityRollRow",
    "AdventureRow",
    "CharacterDocumentRow",
    "CharacterRow",
    "Database",
    "DatabaseOpenError",
    "JsonObject",
    "RowPage",
    "SaveRow",
    "SessionDocumentRow",
    "SessionRow",
    "utc_now",
]

type JsonObject = dict[str, Any]
"""A JSON object as Python decodes it, like a document or a set of ability scores."""

BUSY_TIMEOUT_SECONDS = 5.0
"""How long a statement waits for another connection's write lock before it fails."""

LAYOUT_VERSION = 1
"""The version of the table layout, which the server writes to the file's `PRAGMA user_version`.

A server opens only a file with this version, or an empty file."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ability_rolls (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    client TEXT NOT NULL,
    scores TEXT NOT NULL,
    seed INTEGER NOT NULL,
    used INTEGER NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS characters (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    client TEXT NOT NULL,
    name TEXT NOT NULL,
    class_id TEXT NOT NULL,
    level INTEGER NOT NULL,
    max_hp INTEGER NOT NULL,
    document TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS characters_by_client ON characters (client, created_at, seq);
CREATE TABLE IF NOT EXISTS adventures (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    client TEXT NOT NULL,
    name TEXT NOT NULL,
    info TEXT NOT NULL,
    document TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS adventures_by_client ON adventures (client, created_at, seq);
CREATE TABLE IF NOT EXISTS sessions (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    client TEXT NOT NULL,
    name TEXT NOT NULL,
    adventure_id TEXT NOT NULL,
    adventure_name TEXT NOT NULL,
    mode TEXT NOT NULL,
    revision INTEGER NOT NULL,
    clock_rounds INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    engine_version TEXT NOT NULL,
    document TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS sessions_by_client ON sessions (client, created_at, seq);
CREATE TABLE IF NOT EXISTS saves (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    client TEXT NOT NULL,
    session_id TEXT,
    adventure_name TEXT NOT NULL,
    label TEXT,
    mode TEXT NOT NULL,
    clock_rounds INTEGER NOT NULL,
    document TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS saves_by_client ON saves (client, created_at, seq);
CREATE INDEX IF NOT EXISTS saves_by_session ON saves (client, session_id, created_at, seq);
"""


class DatabaseOpenError(Exception):
    """The server can't use a database file.

    [`Database`][bxapi.db.Database] raises it when SQLite can't open the file, when the file
    isn't a SQLite database, and when the file's table layout version isn't
    [`LAYOUT_VERSION`][bxapi.db.LAYOUT_VERSION]. The message states the path.
    """


def _create_private_file(path: str) -> None:
    # SQLite gives the -wal and -shm files the permissions of the database file, so creating the
    # database file with mode 0600 keeps all three readable and writable only by their owner.
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(descriptor)


def utc_now() -> str:
    """Return the current UTC time in the API's timestamp format.

    Returns:
        An RFC 3339 timestamp with whole seconds, like `"2026-11-20T22:01:05Z"`.

    Examples:
        ```python
        from bxapi.db import utc_now

        stamp = utc_now()
        assert len(stamp) == 20 and stamp.endswith("Z")
        ```
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class RowPage[Row]:
    """One page of rows, newest first, and how many rows match in all."""

    items: list[Row]
    """The rows on this page."""
    total: int
    """How many rows match the query, on every page together."""


@dataclass(frozen=True, slots=True)
class AbilityRollRow:
    """A row of `ability_rolls`: six ability scores rolled before the player picks a class."""

    id: str
    """The roll's id, which starts with `rol_`."""
    scores: JsonObject
    """The scores, as the caller stored them."""
    seed: int
    """The seed the scores were rolled from. Creating a character from the roll draws the rest of
    the creation rolls from the same seed. Never return it to a client."""
    used: bool
    """Whether a character was created from the roll."""
    created_at: str
    """When the roll was stored."""


@dataclass(frozen=True, slots=True)
class CharacterRow:
    """A row of `characters` without its document: the fields of a character summary."""

    id: str
    """The character's id, which starts with `chr_`."""
    name: str
    """The character's name."""
    class_id: str
    """The character's class."""
    level: int
    """The character's level."""
    max_hp: int
    """The character's maximum hit points."""
    created_at: str
    """When the character was stored."""


@dataclass(frozen=True, slots=True)
class CharacterDocumentRow(CharacterRow):
    """A row of `characters` with its document."""

    document: JsonObject
    """The character document."""


@dataclass(frozen=True, slots=True)
class AdventureRow:
    """A row of `adventures` without its document."""

    id: str
    """The adventure's id, which starts with `adv_`."""
    name: str
    """The adventure's name."""
    info: JsonObject
    """The adventure info, as the caller stored it."""
    created_at: str
    """When the adventure was stored."""


@dataclass(frozen=True, slots=True)
class SessionRow:
    """A row of `sessions` without its document: the fields of session info."""

    id: str
    """The session's id, which starts with `ses_`."""
    name: str
    """The session's name."""
    adventure_id: str
    """The id of the adventure the session plays."""
    adventure_name: str
    """The name of the adventure the session plays."""
    mode: str
    """The session's mode after its last accepted command."""
    revision: int
    """How many command requests the session has accepted."""
    clock_rounds: int
    """The game clock, in rounds."""
    schema_version: int
    """The `schema_version` key of the session's document."""
    engine_version: str
    """The `engine_version` key of the session's document."""
    created_at: str
    """When the session was stored."""
    updated_at: str
    """When the session's document was last written."""


@dataclass(frozen=True, slots=True)
class SessionDocumentRow(SessionRow):
    """A row of `sessions` with its document, read in one query so the two match."""

    document: JsonObject
    """The session's save document."""


@dataclass(frozen=True, slots=True)
class SaveRow:
    """A row of `saves` without its document: the fields of save info."""

    id: str
    """The save's id, which starts with `sav_`."""
    session_id: str | None
    """The session the save was made from, or `None` for an imported save."""
    adventure_name: str
    """The name of the adventure the save plays."""
    label: str | None
    """The player's label for the save, or `None` if it has none."""
    mode: str
    """The session's mode when the save was made."""
    clock_rounds: int
    """The game clock when the save was made, in rounds."""
    created_at: str
    """When the save was stored."""


type _Row = AbilityRollRow | CharacterRow | AdventureRow | SessionRow | SaveRow


def _columns(row_type: type[_Row]) -> str:
    return ", ".join(field.name for field in fields(row_type))


def _values(row: _Row) -> dict[str, Any]:
    # `dataclasses.asdict` would deep-copy every document, so copy the top level only.
    return {field.name: getattr(row, field.name) for field in fields(row)}


def _dumps(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _decoded(row: sqlite3.Row, *json_columns: str) -> dict[str, Any]:
    values = dict(row)
    for column in json_columns:
        values[column] = json.loads(values[column])
    return values


def _ability_roll(row: sqlite3.Row) -> AbilityRollRow:
    values = _decoded(row, "scores")
    values["used"] = bool(values["used"])
    return AbilityRollRow(**values)


def _character_row(*, name: str, class_id: str, level: int, max_hp: int, document: JsonObject) -> CharacterDocumentRow:
    return CharacterDocumentRow(
        id=new_id("chr"),
        name=name,
        class_id=class_id,
        level=level,
        max_hp=max_hp,
        created_at=utc_now(),
        document=document,
    )


def _character_values(row: CharacterDocumentRow) -> dict[str, Any]:
    return _values(row) | {"document": _dumps(row.document)}


def _insert_statement(table: str, client: str, values: dict[str, Any]) -> tuple[str, list[Any]]:
    values = {"client": client} | values
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    return f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(values.values())


def _envelope_versions(document: JsonObject) -> tuple[int, str]:
    return int(document["schema_version"]), str(document["engine_version"])


class Database:
    """The server's SQLite database.

    The constructor opens the file, creates the tables in an empty file, and turns on write-ahead
    logging for a file database. It refuses a file whose table layout version isn't
    [`LAYOUT_VERSION`][bxapi.db.LAYOUT_VERSION]. One `Database` uses one connection, and a lock
    lets one thread at a time run a query on it, so every endpoint thread can share it. With `":memory:"`, every thread
    sees the same data, and the data is gone when you close the database.

    Two processes can open the same file. Each method that changes rows is one SQLite
    transaction, so a conditional update, like
    [`update_session`][bxapi.db.Database.update_session], checks and writes the row with no
    other connection in between. A statement waits up to
    [`BUSY_TIMEOUT_SECONDS`][bxapi.db.BUSY_TIMEOUT_SECONDS] for another process to finish a
    write.

    A list is newest first. Rows with the same `created_at` are in the reverse of the order
    they were inserted.

    Args:
        path: The path of the database file, or `":memory:"`. The constructor creates the file
            if it doesn't exist, readable and writable only by its owner (mode 0600), and SQLite
            gives its `-wal` and `-shm` files the same mode.

    Raises:
        DatabaseOpenError: If the file can't be opened, isn't a SQLite database, or has another
            table layout version.

    Examples:
        ```python
        from bxapi.db import Database

        db = Database(":memory:")
        roll = db.insert_ability_roll("local", scores={"str": 12}, seed=7)
        fields = {"name": "Hild", "class_id": "fighter", "level": 1, "max_hp": 8, "document": {}}
        assert db.insert_character_from_ability_roll("local", roll.id, **fields) is not None
        assert db.insert_character_from_ability_roll("local", roll.id, **fields) is None
        assert db.get_ability_roll("other", roll.id) is None
        db.close()
        ```
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        try:
            if path != ":memory:":
                _create_private_file(path)
            self._connection = sqlite3.connect(
                path, timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=False, autocommit=True
            )
        except (OSError, sqlite3.Error) as error:
            raise DatabaseOpenError(f"bxapi can't open the database file {path!r}: {error}") from error
        try:
            self._connection.row_factory = sqlite3.Row
            if path != ":memory:":
                self._use_write_ahead_log()
            self._create_tables(path)
        except sqlite3.Error as error:
            self._connection.close()
            raise DatabaseOpenError(f"bxapi can't use the database file {path!r}: {error}") from error
        except BaseException:
            self._connection.close()
            raise

    def _use_write_ahead_log(self) -> None:
        # While another process creates the same file, the pragma fails at once instead of waiting
        # for the busy timeout. Retry it for as long as a statement waits.
        deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
        while True:
            try:
                self._connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as error:
                busy = error.sqlite_errorcode & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                if not busy or time.monotonic() >= deadline:
                    raise
            time.sleep(0.01)

    def _create_tables(self, path: str) -> None:
        if self._layout_version() == LAYOUT_VERSION:
            return
        with self._write_transaction() as connection:
            # Read the version again under the write lock: another process may have created the tables meanwhile.
            found = self._layout_version()
            if found == LAYOUT_VERSION:
                return
            if found != 0 or connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0]:
                raise DatabaseOpenError(
                    f"The database file {path!r} has table layout version {found}, and this server uses version "
                    f"{LAYOUT_VERSION}. Point BXAPI_DATABASE at a new file, or at a file this version of bxapi created."
                )
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {LAYOUT_VERSION}")

    def _layout_version(self) -> int:
        return self._connection.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        """Close the connection. Any later call on the database raises `sqlite3.ProgrammingError`."""
        with self._lock:
            self._connection.close()

    # Ability rolls

    def insert_ability_roll(self, client: str, *, scores: JsonObject, seed: int) -> AbilityRollRow:
        """Store a new, unused ability roll.

        Args:
            client: The client the roll belongs to.
            scores: The rolled scores.
            seed: The seed the scores were rolled from.

        Returns:
            The stored roll, with a new id.
        """
        row = AbilityRollRow(id=new_id("rol"), scores=scores, seed=seed, used=False, created_at=utc_now())
        values = _values(row) | {"scores": _dumps(scores), "used": 0}
        self._insert("ability_rolls", client, values)
        return row

    def get_ability_roll(self, client: str, roll_id: str) -> AbilityRollRow | None:
        """Return a roll, or `None` if the client has no roll with that id."""
        row = self._one(
            f"SELECT {_columns(AbilityRollRow)} FROM ability_rolls WHERE id = ? AND client = ?", roll_id, client
        )
        return None if row is None else _ability_roll(row)

    # Characters

    def insert_character(
        self, client: str, *, name: str, class_id: str, level: int, max_hp: int, document: JsonObject
    ) -> CharacterDocumentRow:
        """Store a new character.

        Args:
            client: The client the character belongs to.
            name: The character's name.
            class_id: The character's class.
            level: The character's level.
            max_hp: The character's maximum hit points.
            document: The character document.

        Returns:
            The stored character, with a new id.
        """
        row = _character_row(name=name, class_id=class_id, level=level, max_hp=max_hp, document=document)
        self._insert("characters", client, _character_values(row))
        return row

    def insert_character_from_ability_roll(
        self, client: str, roll_id: str, *, name: str, class_id: str, level: int, max_hp: int, document: JsonObject
    ) -> CharacterDocumentRow | None:
        """Mark an unused ability roll used and store the character created from it, in one transaction.

        When two requests create a character from the same roll at once, only one of them
        stores a character. If the insert fails, the roll stays unused. Both hold across threads
        and across processes that share the file. Deleting the character later leaves the roll
        used. Check the character's choices before you call it, so that a refused character
        leaves the roll unused.

        Args:
            client: The client the roll and the character belong to.
            roll_id: The roll's id.
            name: The character's name.
            class_id: The character's class.
            level: The character's level.
            max_hp: The character's maximum hit points.
            document: The character document.

        Returns:
            The stored character, with a new id. `None` if the roll was already used, or the
            client has no roll with that id. In that case the method stores nothing.
        """
        row = _character_row(name=name, class_id=class_id, level=level, max_hp=max_hp, document=document)
        insert, params = _insert_statement("characters", client, _character_values(row))
        with self._write_transaction() as connection:
            claimed = connection.execute(
                "UPDATE ability_rolls SET used = 1 WHERE id = ? AND client = ? AND used = 0", (roll_id, client)
            )
            if claimed.rowcount == 0:
                return None
            connection.execute(insert, params)
        return row

    def get_character(self, client: str, character_id: str) -> CharacterDocumentRow | None:
        """Return a character with its document, or `None` if the client has no character with that id."""
        row = self._one(
            f"SELECT {_columns(CharacterDocumentRow)} FROM characters WHERE id = ? AND client = ?", character_id, client
        )
        return None if row is None else CharacterDocumentRow(**_decoded(row, "document"))

    def list_characters(self, client: str, *, limit: int, offset: int) -> RowPage[CharacterRow]:
        """Return a page of the client's characters, newest first, without their documents.

        Args:
            client: The client whose characters to list.
            limit: The largest number of rows to return.
            offset: How many rows to skip before the first one returned.

        Returns:
            The page, and how many characters the client has.
        """
        rows, total = self._page(_columns(CharacterRow), "characters", "client = ?", [client], limit, offset)
        return RowPage(items=[CharacterRow(**dict(row)) for row in rows], total=total)

    def delete_character(self, client: str, character_id: str) -> bool:
        """Delete a character. Return `False` if the client has no character with that id."""
        return self._change("DELETE FROM characters WHERE id = ? AND client = ?", character_id, client)

    # Adventures

    def insert_adventure(self, client: str, *, name: str, info: JsonObject, document: JsonObject) -> AdventureRow:
        """Store a new adventure.

        Args:
            client: The client the adventure belongs to.
            name: The adventure's name.
            info: The adventure info to return to players.
            document: The adventure document.

        Returns:
            The stored adventure, with a new id.
        """
        row = AdventureRow(id=new_id("adv"), name=name, info=info, created_at=utc_now())
        values = _values(row) | {"info": _dumps(info), "document": _dumps(document)}
        self._insert("adventures", client, values)
        return row

    def get_adventure(self, client: str, adventure_id: str) -> AdventureRow | None:
        """Return an adventure without its document, or `None` if the client has no adventure with that id."""
        row = self._one(
            f"SELECT {_columns(AdventureRow)} FROM adventures WHERE id = ? AND client = ?", adventure_id, client
        )
        return None if row is None else AdventureRow(**_decoded(row, "info"))

    def get_adventure_document(self, client: str, adventure_id: str) -> JsonObject | None:
        """Return an adventure's document, or `None` if the client has no adventure with that id."""
        row = self._one("SELECT document FROM adventures WHERE id = ? AND client = ?", adventure_id, client)
        return None if row is None else json.loads(row["document"])

    def list_adventures(self, client: str, *, limit: int, offset: int) -> RowPage[AdventureRow]:
        """Return a page of the client's adventures, newest first, without their documents.

        Args:
            client: The client whose adventures to list.
            limit: The largest number of rows to return.
            offset: How many rows to skip before the first one returned.

        Returns:
            The page, and how many adventures the client has stored.
        """
        rows, total = self._page(_columns(AdventureRow), "adventures", "client = ?", [client], limit, offset)
        return RowPage(items=[AdventureRow(**_decoded(row, "info")) for row in rows], total=total)

    def delete_adventure(self, client: str, adventure_id: str) -> bool:
        """Delete an adventure. Return `False` if the client has no adventure with that id."""
        return self._change("DELETE FROM adventures WHERE id = ? AND client = ?", adventure_id, client)

    # Sessions

    def insert_session(
        self,
        client: str,
        *,
        name: str,
        adventure_id: str,
        adventure_name: str,
        mode: str,
        clock_rounds: int,
        document: JsonObject,
    ) -> SessionRow:
        """Store a new session at revision 0.

        The method copies `schema_version` and `engine_version` from the document's keys into
        their columns, so a caller can build session info without reading the document.

        Args:
            client: The client the session belongs to.
            name: The session's name.
            adventure_id: The id of the adventure the session plays.
            adventure_name: The name of the adventure the session plays.
            mode: The session's mode.
            clock_rounds: The game clock, in rounds.
            document: The session's save document.

        Returns:
            The stored session, with a new id. `created_at` and `updated_at` are the same.

        Raises:
            KeyError: If the document has no `schema_version` or `engine_version` key.
        """
        schema_version, engine_version = _envelope_versions(document)
        now = utc_now()
        row = SessionRow(
            id=new_id("ses"),
            name=name,
            adventure_id=adventure_id,
            adventure_name=adventure_name,
            mode=mode,
            revision=0,
            clock_rounds=clock_rounds,
            schema_version=schema_version,
            engine_version=engine_version,
            created_at=now,
            updated_at=now,
        )
        self._insert("sessions", client, _values(row) | {"document": _dumps(document)})
        return row

    def get_session(self, client: str, session_id: str) -> SessionRow | None:
        """Return a session without its document, or `None` if the client has no session with that id."""
        row = self._one(f"SELECT {_columns(SessionRow)} FROM sessions WHERE id = ? AND client = ?", session_id, client)
        return None if row is None else SessionRow(**dict(row))

    def get_session_with_document(self, client: str, session_id: str) -> SessionDocumentRow | None:
        """Return a session with its document, or `None` if the client has no session with that id.

        The row and the document come from one query, so `revision` is the revision of that
        document.
        """
        row = self._one(
            f"SELECT {_columns(SessionDocumentRow)} FROM sessions WHERE id = ? AND client = ?", session_id, client
        )
        return None if row is None else SessionDocumentRow(**_decoded(row, "document"))

    def get_session_revision(self, client: str, session_id: str) -> int | None:
        """Return a session's revision, or `None` if the client has no session with that id."""
        row = self._one("SELECT revision FROM sessions WHERE id = ? AND client = ?", session_id, client)
        return None if row is None else row["revision"]

    def list_sessions(self, client: str, *, limit: int, offset: int, mode: str | None = None) -> RowPage[SessionRow]:
        """Return a page of the client's sessions, newest first, without their documents.

        Args:
            client: The client whose sessions to list.
            limit: The largest number of rows to return.
            offset: How many rows to skip before the first one returned.
            mode: Return only the sessions in this mode. `None` returns sessions in every mode.

        Returns:
            The page, and how many of the client's sessions match `mode`.
        """
        where, params = "client = ?", [client]
        if mode is not None:
            where, params = "client = ? AND mode = ?", [client, mode]
        rows, total = self._page(_columns(SessionRow), "sessions", where, params, limit, offset)
        return RowPage(items=[SessionRow(**dict(row)) for row in rows], total=total)

    def rename_session(self, client: str, session_id: str, name: str) -> bool:
        """Change a session's name. Return `False` if the client has no session with that id.

        The name isn't part of the game, so the call changes neither `revision` nor `updated_at`.
        """
        return self._change("UPDATE sessions SET name = ? WHERE id = ? AND client = ?", name, session_id, client)

    def update_session(
        self,
        client: str,
        session_id: str,
        *,
        expected_revision: int,
        document: JsonObject,
        mode: str,
        clock_rounds: int,
        revision: int,
        updated_at: str,
    ) -> bool:
        """Write a session's new state, if the session still has the expected revision.

        The check and the write are one statement, so they're correct across threads and across
        processes that share the file. When two writers start from the same revision, one gets
        `True` and the other gets `False` and changes nothing. The method copies `schema_version`
        and `engine_version` from the document's keys into their columns.

        Args:
            client: The client the session belongs to.
            session_id: The session's id.
            expected_revision: The revision the session must have for the write to happen.
            document: The session's new save document.
            mode: The session's new mode.
            clock_rounds: The new game clock, in rounds.
            revision: The session's new revision.
            updated_at: The time of the write, from [`utc_now`][bxapi.db.utc_now].

        Returns:
            `True` if the session had `expected_revision` and the call wrote the new state.
            `False` if its revision was different, or the client has no session with that id.

        Raises:
            KeyError: If the document has no `schema_version` or `engine_version` key.
        """
        schema_version, engine_version = _envelope_versions(document)
        return self._change(
            "UPDATE sessions SET document = ?, mode = ?, clock_rounds = ?, revision = ?, schema_version = ?,"
            " engine_version = ?, updated_at = ? WHERE id = ? AND client = ? AND revision = ?",
            _dumps(document),
            mode,
            clock_rounds,
            revision,
            schema_version,
            engine_version,
            updated_at,
            session_id,
            client,
            expected_revision,
        )

    def delete_session(self, client: str, session_id: str) -> bool:
        """Delete a session and leave its saves. Return `False` if the client has no session with that id."""
        return self._change("DELETE FROM sessions WHERE id = ? AND client = ?", session_id, client)

    # Saves

    def insert_save(
        self,
        client: str,
        *,
        session_id: str | None,
        adventure_name: str,
        label: str | None,
        mode: str,
        clock_rounds: int,
        document: JsonObject,
    ) -> SaveRow:
        """Store a new save.

        Args:
            client: The client the save belongs to.
            session_id: The session the save was made from, or `None` for an imported save.
            adventure_name: The name of the adventure the save plays.
            label: The player's label, or `None`.
            mode: The session's mode when the save was made.
            clock_rounds: The game clock when the save was made, in rounds.
            document: The save document.

        Returns:
            The stored save, with a new id.
        """
        row = SaveRow(
            id=new_id("sav"),
            session_id=session_id,
            adventure_name=adventure_name,
            label=label,
            mode=mode,
            clock_rounds=clock_rounds,
            created_at=utc_now(),
        )
        self._insert("saves", client, _values(row) | {"document": _dumps(document)})
        return row

    def get_save(self, client: str, save_id: str) -> SaveRow | None:
        """Return a save without its document, or `None` if the client has no save with that id."""
        row = self._one(f"SELECT {_columns(SaveRow)} FROM saves WHERE id = ? AND client = ?", save_id, client)
        return None if row is None else SaveRow(**dict(row))

    def get_save_document(self, client: str, save_id: str) -> JsonObject | None:
        """Return a save's document, or `None` if the client has no save with that id."""
        row = self._one("SELECT document FROM saves WHERE id = ? AND client = ?", save_id, client)
        return None if row is None else json.loads(row["document"])

    def list_saves(self, client: str, *, limit: int, offset: int, session_id: str | None = None) -> RowPage[SaveRow]:
        """Return a page of the client's saves, newest first, without their documents.

        Args:
            client: The client whose saves to list.
            limit: The largest number of rows to return.
            offset: How many rows to skip before the first one returned.
            session_id: Return only the saves made from this session. `None` returns every save.

        Returns:
            The page, and how many of the client's saves match `session_id`.
        """
        where, params = "client = ?", [client]
        if session_id is not None:
            where, params = "client = ? AND session_id = ?", [client, session_id]
        rows, total = self._page(_columns(SaveRow), "saves", where, params, limit, offset)
        return RowPage(items=[SaveRow(**dict(row)) for row in rows], total=total)

    def update_save_label(self, client: str, save_id: str, label: str | None) -> bool:
        """Change a save's label. Return `False` if the client has no save with that id."""
        return self._change("UPDATE saves SET label = ? WHERE id = ? AND client = ?", label, save_id, client)

    def delete_save(self, client: str, save_id: str) -> bool:
        """Delete a save. Return `False` if the client has no save with that id."""
        return self._change("DELETE FROM saves WHERE id = ? AND client = ?", save_id, client)

    # The methods above build SQL only from the constants in this module, and pass every value
    # from a caller as a parameter.

    def _insert(self, table: str, client: str, values: dict[str, Any]) -> None:
        sql, params = _insert_statement(table, client, values)
        with self._lock:
            self._connection.execute(sql, params)

    def _one(self, sql: str, *params: object) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, params).fetchone()

    def _change(self, sql: str, *params: object) -> bool:
        with self._lock:
            return self._connection.execute(sql, params).rowcount > 0

    def _page(
        self, columns: str, table: str, where: str, params: Sequence[object], limit: int, offset: int
    ) -> tuple[list[sqlite3.Row], int]:
        with self._read_transaction() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
            rows = connection.execute(
                f"SELECT {columns} FROM {table} WHERE {where} ORDER BY created_at DESC, seq DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return rows, total

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        # BEGIN IMMEDIATE takes the file's write lock before the first statement, so another
        # process can't change a row between this transaction's check and its write.
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    @contextmanager
    def _read_transaction(self) -> Iterator[sqlite3.Connection]:
        # One transaction makes the count and the page read the same snapshot, even while
        # another process writes.
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield self._connection
            finally:
                self._connection.execute("COMMIT")
