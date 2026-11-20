"""HTTP API for the B/X rules, built with FastAPI on osrlib.

Build the application with [`create_app`][bxapi.app.create_app], or run the `bxapi` command to
serve it with uvicorn.
"""

from importlib.metadata import version

__all__ = ["__version__"]

__version__ = version("bxapi")
"""The installed version of bxapi, like `"0.1.0"`."""
