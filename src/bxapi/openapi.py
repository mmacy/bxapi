"""Print the OpenAPI document, so you can generate a client without running a server.

```sh
uv run python -m bxapi.openapi > openapi.json
```

The command builds the application with settings it defines itself: an in-memory database,
anonymous callers allowed, and no API keys. It reads no `BXAPI_` environment variable and no
`.env` file, and it creates no file, so the document is the same on every machine that has the
same release of bxapi. The server's `/openapi.json` returns the same document.
"""

import json
from typing import Any

from fastapi import FastAPI
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from bxapi.app import create_app
from bxapi.settings import Settings

__all__ = ["build_openapi", "main"]


class _FixedSettings(Settings):
    """The settings with every source but the constructor's arguments switched off."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def build_openapi() -> dict[str, Any]:
    """Return the application's OpenAPI document.

    Returns:
        The document as a dict that `json.dumps` can write.

    Examples:
        ```python
        from bxapi.openapi import build_openapi

        document = build_openapi()
        assert document["info"]["title"] == "bxapi"
        assert "/v1/sessions" in document["paths"]
        ```
    """
    app: FastAPI = create_app(_FixedSettings(database=":memory:", allow_anonymous=True))
    try:
        return app.openapi()
    finally:
        app.state.db.close()


def main() -> None:
    """Print the OpenAPI document as JSON on standard output."""
    print(json.dumps(build_openapi(), indent=2))


if __name__ == "__main__":
    main()
