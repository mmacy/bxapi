"""The `bxapi` command, which serves the API with uvicorn."""

import uvicorn

from bxapi.app import create_app
from bxapi.settings import Settings

__all__ = ["main"]


def main() -> None:
    """Serve the API on the host and port in the settings.

    The command takes no arguments. Set `BXAPI_HOST`, `BXAPI_PORT`, and the other `BXAPI_`
    environment variables to configure it. It runs one process, and it runs until you stop it.
    """
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
