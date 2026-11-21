"""The `bxapi` command, which serves the API with uvicorn."""

import copy
import json
import sys

import uvicorn
from pydantic import ValidationError
from pydantic_core import ErrorDetails
from pydantic_settings import SettingsError as EnvironmentSettingsError
from uvicorn.config import LOGGING_CONFIG

from bxapi.app import create_app
from bxapi.db import DatabaseOpenError
from bxapi.settings import Settings, SettingsError

__all__ = ["main"]

_VALUE_ERROR_PREFIX = "Value error, "

_NOT_JSON = (
    "bxapi: a BXAPI_ variable that takes a list isn't valid JSON, and an empty value isn't valid JSON either. "
    "BXAPI_API_KEYS takes a JSON list, like "
    """'[{"client": "my-game", "key": "<at least 16 characters>", "role": "player"}]', and """
    """BXAPI_CORS_ORIGINS takes a JSON list, like '["https://game.example"]'."""
)


def _describe(problem: ErrorDetails) -> str:
    """Write one settings problem as a line that names the environment variable.

    The first element of the problem's location is the field name. Each later element is a
    position in a list (written as a 1-based entry number) or a field of an entry. The line never
    contains the value that failed.
    """
    message = problem["msg"].removeprefix(_VALUE_ERROR_PREFIX)
    location = problem["loc"]
    if not location:
        return message
    parts = [f"BXAPI_{str(location[0]).upper()}"]
    parts += [f"entry {part + 1}" if isinstance(part, int) else str(part) for part in location[1:]]
    return f"{', '.join(parts)}: {message}"


def main() -> None:
    """Serve the API on the host and port in the settings.

    The command takes no arguments. Set `BXAPI_HOST`, `BXAPI_PORT`, and the other `BXAPI_`
    environment variables to configure it. It runs one process, and it runs until you stop it.
    It sends bxapi's log lines, like the one about API keys at startup, to the same place as
    uvicorn's. bxapi logs one line for each request, with the request's id, so the command turns
    uvicorn's own line for each request off.

    When the settings are invalid, the database can't be opened, or the server has no API keys
    and `BXAPI_ALLOW_ANONYMOUS` isn't set, the command prints why and exits with status 1. For an
    invalid setting, it prints one line for each problem, like
    `bxapi: BXAPI_SESSION_CACHE_SIZE: Input should be greater than or equal to 1`, and never
    prints the value. For a list variable that isn't valid JSON, it prints one line that names
    `BXAPI_API_KEYS` and `BXAPI_CORS_ORIGINS` with an example of each.
    """
    try:
        settings = Settings()
        app = create_app(settings)
    except ValidationError as error:
        for problem in error.errors(include_url=False, include_context=False, include_input=False):
            print(f"bxapi: {_describe(problem)}", file=sys.stderr)
        raise SystemExit(1) from None
    except EnvironmentSettingsError as error:
        # pydantic-settings puts the field's name only in the error's text, so the line names both list variables
        # instead of parsing that text. The cause is the JSON error, which never contains the value.
        if not isinstance(error.__cause__, json.JSONDecodeError):
            raise
        print(_NOT_JSON, file=sys.stderr)
        raise SystemExit(1) from None
    except (SettingsError, DatabaseOpenError) as error:
        raise SystemExit(f"bxapi: {error}") from None
    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["loggers"]["bxapi"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=log_config, access_log=False)


if __name__ == "__main__":
    main()
