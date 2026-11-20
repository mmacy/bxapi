"""Server settings, read from `BXAPI_` environment variables.

The server reads every setting once, when [`create_app`][bxapi.app.create_app] builds the
application. To change a setting, set its environment variable and restart the server. A `.env`
file in the working directory supplies any variable that the environment doesn't set.

A setting with a list value is written as JSON:

```sh
export BXAPI_CORS_ORIGINS='["https://game.example"]'
export BXAPI_API_KEYS='[{"client": "my-game", "key": "s3cret", "role": "player"}]'
```
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ApiKey", "Role", "Settings"]


class Role(StrEnum):
    """What a caller may do. A referee may do everything a player may."""

    PLAYER = "player"
    """Plays games. The server shows a player only what the players of a game may see, and
    refuses a player's referee commands."""
    REFEREE = "referee"
    """Runs games. A referee can read a session's hidden state, issue referee commands, choose a
    session's seed, and move full adventure and save documents in and out of the server. Give
    this role to a server you trust, never to a client a player controls."""


class ApiKey(BaseModel):
    """One API key, the client application it identifies, and the role it grants."""

    model_config = ConfigDict(frozen=True)

    client: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    """The name of the client application. Every character, adventure, session, and save belongs
    to the client that created it, and no other client can read it. Two keys with the same
    client name share that data, so you can replace a key without losing anything."""
    key: SecretStr = Field(min_length=16)
    """The secret the client sends as a bearer token. It must be at least 16 characters long."""
    role: Role = Role.PLAYER
    """The role the key grants."""


class Settings(BaseSettings):
    """Every setting the server reads, with its default.

    Examples:
        ```python
        from bxapi.settings import Settings

        settings = Settings(database=":memory:")
        assert settings.port == 8640
        assert settings.api_keys == ()
        ```
    """

    model_config = SettingsConfigDict(env_prefix="BXAPI_", env_file=".env", extra="ignore", frozen=True)

    database: str = "bxapi.db"
    """The path of the SQLite database file. The server creates the file if it doesn't exist.
    `":memory:"` stores everything in memory and loses it when the server stops."""
    api_keys: tuple[ApiKey, ...] = ()
    """The API keys the server accepts. When the tuple is empty, the server asks for no key and
    treats every caller as a referee for the client named `local`. Use that only on your own
    machine."""
    cors_origins: tuple[str, ...] = ()
    """The web origins whose pages may call the API from a browser, like `"https://game.example"`.
    When the tuple is empty, the server sends no cross-origin headers."""
    session_cache_size: int = Field(default=256, ge=1)
    """How many sessions the server keeps loaded in memory. When the count passes this number,
    the server drops the session that was used longest ago and loads it from the database the
    next time a request needs it."""
    max_body_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    """The largest request body the server accepts, in bytes. A larger request gets a 413."""
    host: str = "127.0.0.1"
    """The address the `bxapi` command listens on."""
    port: int = Field(default=8640, ge=0, le=65535)
    """The port the `bxapi` command listens on."""

    @model_validator(mode="after")
    def _keys_are_distinct(self) -> Settings:
        secrets = [entry.key.get_secret_value() for entry in self.api_keys]
        if len(set(secrets)) != len(secrets):
            raise ValueError("two entries in api_keys have the same key")
        return self

    @property
    def auth_enabled(self) -> bool:
        """Whether the server asks callers for an API key."""
        return bool(self.api_keys)
