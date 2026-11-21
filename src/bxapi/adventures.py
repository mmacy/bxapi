"""Adventure documents: reading one, writing one, and the adventures that come with the server.

An *adventure document* is osrlib's `Adventure` model inside osrlib's versioned envelope, with
`kind` set to `"adventure"`. Tools like osr-forge and osr-editor write these documents, and a
client uploads one to play it.

[`parse_adventure`][bxapi.adventures.parse_adventure] turns a document into an `Adventure`
after it runs every check osrlib has on one, so an adventure that parses can start a session.
[`builtin_adventures`][bxapi.adventures.builtin_adventures] returns the adventures in the
[`bxapi.data`][bxapi.data] package, which every client can read and none can delete.
[`find_adventure`][bxapi.adventures.find_adventure] looks an id up in both places.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from types import MappingProxyType

from osrlib.crawl.adventure import Adventure, validate_adventure
from osrlib.data import load_equipment, load_monsters
from osrlib.errors import ContentValidationError, SaveVersionError
from osrlib.versioning import check_document, stamp_document
from pydantic import ValidationError

from bxapi.db import Database
from bxapi.errors import MAX_DETAIL_LENGTH, not_found, shorten

__all__ = [
    "ADVENTURE_KIND",
    "BUILTIN_PREFIX",
    "BuiltinAdventure",
    "adventure_document",
    "builtin_adventures",
    "find_adventure",
    "is_builtin",
    "parse_adventure",
]

ADVENTURE_KIND = "adventure"
"""The `kind` of an adventure document."""

BUILTIN_PREFIX = "builtin-"
"""The start of the id of every adventure that comes with the server."""

_MAX_PROBLEMS = 20
_MAX_PROBLEM_LENGTH = 128


_INVALID = "The document isn't a valid adventure. "
# Room for "; and N more" with a count of up to 15 digits.
_MORE_ROOM = 30


def _invalid_message(error: ValidationError) -> str:
    entries = error.errors()
    listed: list[str] = []
    length = len(_INVALID)
    for entry in entries[:_MAX_PROBLEMS]:
        location = ".".join(str(part) for part in entry["loc"]) or "payload"
        problem = shorten(f"{location}: {entry['msg']}", _MAX_PROBLEM_LENGTH)
        if length + len(problem) + 2 + _MORE_ROOM > MAX_DETAIL_LENGTH:
            break
        listed.append(problem)
        length += len(problem) + 2
    if len(entries) > len(listed):
        listed.append(f"and {len(entries) - len(listed)} more")
    return _INVALID + "; ".join(listed)


def parse_adventure(document: Mapping[str, object]) -> Adventure:
    """Return the adventure in a document, after every check osrlib makes on one.

    The function checks the envelope, then the adventure's structure, then every reference the
    adventure makes to a monster, an item, an area, or a quest. The message of the error it
    raises is at most 1,000 characters long, because osrlib's messages can repeat parts of the
    document. For a payload that isn't a valid adventure, the message lists at most 20
    problems, followed by how many more there are.

    Args:
        document: An adventure document, as decoded JSON.

    Returns:
        The adventure, ready to pass to osrlib's `GameSession.new`.

    Raises:
        osrlib.errors.ContentValidationError: If the envelope is malformed or of another kind,
            if the payload isn't a valid adventure, or if the adventure refers to something that
            doesn't exist. The API sends it as 422 `content_invalid`.
        osrlib.errors.SaveVersionError: If the document is stamped with a schema version newer
            than the installed osrlib reads. The API sends it as 409 `version_unsupported`.

    Examples:
        ```python
        from bxapi.adventures import adventure_document, builtin_adventures, parse_adventure

        barrow = builtin_adventures()["builtin-barrow"].adventure
        assert parse_adventure(adventure_document(barrow)) == barrow
        ```
    """
    try:
        payload = check_document(document, ADVENTURE_KIND)
        try:
            adventure = Adventure.model_validate(payload)
        except ValidationError as error:
            raise ContentValidationError(_invalid_message(error)) from error
        validate_adventure(adventure, load_monsters(), load_equipment())
    except (ContentValidationError, SaveVersionError) as error:
        message = str(error)
        if len(message) <= MAX_DETAIL_LENGTH:
            raise
        raise type(error)(shorten(message, MAX_DETAIL_LENGTH)) from error
    return adventure


def adventure_document(adventure: Adventure) -> dict[str, object]:
    """Return an adventure as a document stamped with the installed osrlib's versions.

    Args:
        adventure: The adventure to write out.

    Returns:
        A JSON-ready dict with the keys `kind`, `schema_version`, `engine_version`, and
        `payload`.
    """
    return stamp_document(ADVENTURE_KIND, adventure.model_dump(mode="json"))


@dataclass(frozen=True)
class BuiltinAdventure:
    """One adventure that comes with the server."""

    id: str
    """The adventure's id, like `"builtin-barrow"`."""
    document: Mapping[str, object]
    """The adventure document, as the file in [`bxapi.data`][bxapi.data] contains it."""
    adventure: Adventure
    """The adventure the document contains."""


def is_builtin(adventure_id: str) -> bool:
    """Return whether an id belongs to an adventure that comes with the server.

    Args:
        adventure_id: An adventure id from a request.

    Returns:
        `True` if the id starts with [`BUILTIN_PREFIX`][bxapi.adventures.BUILTIN_PREFIX]. The
        id may still match no adventure.
    """
    return adventure_id.startswith(BUILTIN_PREFIX)


@cache
def builtin_adventures() -> Mapping[str, BuiltinAdventure]:
    """Return the adventures that come with the server, by id, in id order.

    The function reads every `.json` file in [`bxapi.data`][bxapi.data] the first time you call
    it, and returns the same read-only mapping after that.

    Returns:
        A mapping from each id to its [`BuiltinAdventure`][bxapi.adventures.BuiltinAdventure].

    Raises:
        osrlib.errors.ContentValidationError: If a file in the package isn't a valid adventure
            document for the installed osrlib. That's a defect in bxapi, and the tests check
            for it.

    Examples:
        ```python
        from bxapi.adventures import builtin_adventures

        barrow = builtin_adventures()["builtin-barrow"]
        assert barrow.adventure.name == "The Barrow of the Forgotten King"
        ```
    """
    found: dict[str, BuiltinAdventure] = {}
    files = sorted(
        (entry for entry in resources.files("bxapi.data").iterdir() if entry.name.endswith(".json")),
        key=lambda entry: entry.name,
    )
    for entry in files:
        document = json.loads(entry.read_text(encoding="utf-8"))
        adventure_id = BUILTIN_PREFIX + entry.name.removesuffix(".json")
        found[adventure_id] = BuiltinAdventure(
            id=adventure_id, document=MappingProxyType(document), adventure=parse_adventure(document)
        )
    return MappingProxyType(found)


def find_adventure(db: Database, client: str, adventure_id: str) -> Adventure:
    """Return the adventure with an id: one that comes with the server, or one the client stored.

    Call it to start a session. The function parses a stored document again, so an adventure
    stored under an older osrlib is checked against the installed one.

    Args:
        db: The database.
        client: The name of the caller's client application.
        adventure_id: The id from the request.

    Returns:
        The adventure.

    Raises:
        ApiError: With status 404 and the code `not_found`, if the id matches no built-in
            adventure and no adventure the client stored.
        osrlib.errors.ContentValidationError: If the stored document no longer passes the
            installed osrlib's checks.
    """
    builtin = builtin_adventures().get(adventure_id)
    if builtin is not None:
        return builtin.adventure
    document = None if is_builtin(adventure_id) else db.get_adventure_document(client, adventure_id)
    if document is None:
        raise not_found("adventure", adventure_id)
    return parse_adventure(document)
