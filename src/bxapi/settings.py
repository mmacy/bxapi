"""Server settings, read from `BXAPI_` environment variables.

The server reads every setting once, when [`create_app`][bxapi.app.create_app] builds the
application. To change a setting, set its environment variable and restart the server. For a
variable that the environment doesn't set, the server reads a `.env` file in the working directory.

A setting with a list value is written as JSON:

```sh
export BXAPI_CORS_ORIGINS='["https://game.example"]'
export BXAPI_API_KEYS='[{"client": "my-game", "key": "s3cret-0123456789", "role": "player"}]'
```

A value the server can't accept stops it from starting. The error message doesn't repeat the
value, so a key in a malformed setting doesn't end up in a log.
"""

import re
from enum import StrEnum
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ApiKey", "Role", "Settings", "SettingsError"]

_KEY_CHARACTERS = re.compile(r"[\x21-\x7e]+")
_MIN_KEY_LENGTH = 16

_ORIGIN_RULE = (
    "each CORS origin must be a scheme, a host, and an optional port, like 'https://game.example' or "
    "'http://localhost:3000', with no path and no trailing slash"
)


class SettingsError(Exception):
    """The settings are valid one by one, but the server can't run with them together.

    [`create_app`][bxapi.app.create_app] raises it. The message states what to set.
    """


class Role(StrEnum):
    """What an API key may do in a game: `player` or `referee`.

    - `player`: plays games and sees only player-visible state and events.
    - `referee`: everything `player` may do, plus the referee view, referee events, referee
      commands, chosen seeds, character and save imports, and full adventure and save documents.

    A role restricts visibility and actions inside a game, not data ownership. Every key for a
    client application can read and modify all of that client's characters, adventures, sessions,
    and saves, regardless of role.
    """

    PLAYER = "player"
    """Plays games. The server shows a player only what the players of a game may see, and
    refuses a player's referee commands."""
    REFEREE = "referee"
    """Runs games. A referee can read a session's hidden state, issue referee commands, choose a
    session's seed, and move full adventure and save documents in and out of the server. Give
    this role only to software whose key the players can't read, like your own game server."""


class ApiKey(BaseModel):
    """One API key, the client application it identifies, and the role it grants."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    client: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    """The name of the client application. bxapi has no user accounts: a key identifies an
    application, not a person. Every character, adventure, session, and save belongs to the
    client that created it, and no other client can read it. Everyone who has one of a client's
    keys shares all of that client's data, whatever the key's role. Two keys with the same
    client name share it too, so you can replace a key without losing anything. An application
    with many users either gives each user only the ids of that user's resources, which are
    random and can't be guessed, or sends the users' requests to bxapi through a server that the
    application runs."""
    key: SecretStr
    """The secret the client sends as a bearer token: at least 16 printable ASCII characters,
    with no spaces."""
    role: Role = Role.PLAYER
    """The role the key grants."""

    @field_validator("key")
    @classmethod
    def _key_is_long_enough_and_fits_in_a_header(cls, key: SecretStr) -> SecretStr:
        secret = key.get_secret_value()
        if len(secret) < _MIN_KEY_LENGTH:
            raise ValueError(f"the key must be at least {_MIN_KEY_LENGTH} characters long")
        if not _KEY_CHARACTERS.fullmatch(secret):
            raise ValueError("an API key may contain only printable ASCII characters, with no spaces")
        return key


def _is_origin(origin: str) -> bool:
    try:
        parts = urlsplit(origin)
        return (
            bool(parts.scheme and parts.hostname)
            and parts.username is None
            and (parts.port is None or parts.port > 0)
            and f"{parts.scheme}://{parts.netloc}" == origin
        )
    except ValueError:
        # urlsplit raises ValueError for a malformed host, and so does `port` for a port that isn't a number from 0
        # to 65535.
        return False


class Settings(BaseSettings):
    """Every setting the server reads, with its default.

    Examples:
        ```python
        from bxapi.settings import Settings

        settings = Settings(database=":memory:", allow_anonymous=True)
        assert settings.port == 8640
        assert settings.api_keys == ()
        ```
    """

    model_config = SettingsConfigDict(
        env_prefix="BXAPI_", env_file=".env", extra="ignore", frozen=True, hide_input_in_errors=True
    )

    database: str = "bxapi.db"
    """The path of the SQLite database file. The server creates the file if it doesn't exist,
    readable and writable only by the user that runs the server. `":memory:"` stores everything
    in memory and loses it when the server stops."""
    api_keys: tuple[ApiKey, ...] = ()
    """The API keys the server accepts. With no keys, the server starts only when
    `allow_anonymous` is `true`."""
    allow_anonymous: bool = False
    """Whether a server with no API keys may start. Such a server asks for no key and treats
    every caller as a referee for the client named `local`, so use it only on your own machine.
    The setting has no effect when `api_keys` lists any key."""
    cors_origins: tuple[str, ...] = ()
    """The web origins whose pages may call the API from a browser, like `"https://game.example"`.
    Each is a scheme, a host, and an optional port, with no path and no trailing slash. When the
    tuple is empty, the server sends no cross-origin headers."""
    rate_limit_per_minute: int = Field(default=600, ge=0)
    """How many requests under `/v1` each client may send in a minute, in each server process. A
    client can send that many at once, and regains them at a steady rate. A request over the
    limit gets a 429. Requests with no valid API key share one budget of the same size. 0 turns
    the limit off."""
    session_cache_size: int = Field(default=256, ge=1)
    """How many sessions the server keeps loaded in memory. When the count passes this number,
    the server drops the session that was used longest ago and loads it from the database the
    next time a request needs it."""
    max_client_bytes: int = Field(default=1024**3, ge=0)
    """The most bytes of documents the server stores for one client: its characters, adventures,
    sessions, and saves, and the responses it stores for idempotency keys. A client at the limit
    gets a 507 for each request that would store more, until it deletes something. 0 turns the
    limit off."""
    max_body_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    """The largest request body the server accepts, in bytes. A larger request gets a 413."""
    body_timeout_seconds: float = Field(default=30, gt=0)
    """How long the server waits for the whole body of a request to arrive, in seconds. A request
    whose body takes longer gets a 408."""
    docs_enabled: bool = True
    """Whether the server serves `/docs`, `/redoc`, and `/openapi.json`, which need no API key."""
    host: str = "127.0.0.1"
    """The address the `bxapi` command listens on."""
    port: int = Field(default=8640, ge=0, le=65535)
    """The port the `bxapi` command listens on."""

    @field_validator("cors_origins")
    @classmethod
    def _origins_are_origins(cls, origins: tuple[str, ...]) -> tuple[str, ...]:
        if not all(_is_origin(origin) for origin in origins):
            raise ValueError(_ORIGIN_RULE)
        return origins

    @model_validator(mode="after")
    def _keys_are_distinct(self) -> Self:
        secrets = [entry.key.get_secret_value() for entry in self.api_keys]
        if len(set(secrets)) != len(secrets):
            raise ValueError("two entries in BXAPI_API_KEYS have the same key")
        return self

    @property
    def auth_enabled(self) -> bool:
        """Whether the server asks callers for an API key."""
        return bool(self.api_keys)
