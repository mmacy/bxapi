"""The `bxapi` command, which serves the API with uvicorn."""

import copy

import uvicorn
from pydantic import ValidationError
from uvicorn.config import LOGGING_CONFIG

from bxapi.app import create_app
from bxapi.db import DatabaseOpenError
from bxapi.settings import Settings, SettingsError

__all__ = ["main"]


def main() -> None:
    """Serve the API on the host and port in the settings.

    The command takes no arguments. Set `BXAPI_HOST`, `BXAPI_PORT`, and the other `BXAPI_`
    environment variables to configure it. It runs one process, and it runs until you stop it.
    It logs the server's own lines, like the one about API keys at startup, with uvicorn's.

    When the settings are invalid, the database can't be opened, or the server has no API keys
    and `BXAPI_ALLOW_ANONYMOUS` isn't set, the command prints why and exits with status 1.
    """
    try:
        settings = Settings()
        app = create_app(settings)
    except (ValidationError, SettingsError, DatabaseOpenError) as error:
        raise SystemExit(f"bxapi: {error}") from None
    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["loggers"]["bxapi"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=log_config)


if __name__ == "__main__":
    main()
