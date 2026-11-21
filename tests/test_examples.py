"""The example client in `examples/` plays the built-in adventure against a running server."""

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from bxapi.app import create_app
from bxapi.settings import Settings
from tests.conftest import PLAYER_KEY

_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "play_barrow.py"


def _serve(settings: Settings) -> Iterator[str]:
    """Run the application on a free local port in a background thread, and yield its URL."""
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the server didn't start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def open_url(settings: Settings) -> Iterator[str]:
    """The URL of a server that asks for no API key."""
    yield from _serve(settings)


@pytest.fixture
def keyed_url(keyed_settings: Settings) -> Iterator[str]:
    """The URL of a server that asks for an API key."""
    yield from _serve(keyed_settings)


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(_SCRIPT), *arguments], capture_output=True, text=True, timeout=60)


def test_example_plays_to_the_end_of_the_first_battle(open_url: str) -> None:
    result = _run(open_url)
    assert result.returncode == 0, result.stderr
    assert "The party enters dungeon barrow" in result.stdout
    assert "Encounter: 2 goblins" in result.stdout
    assert "> resolve_battle_round" in result.stdout
    assert "The battle is over." in result.stdout


def test_example_sends_the_api_key(keyed_url: str) -> None:
    result = _run(keyed_url, "--api-key", PLAYER_KEY)
    assert result.returncode == 0, result.stderr
    assert "Started The Barrow of the Forgotten King" in result.stdout


def test_example_stops_after_the_command_limit(open_url: str) -> None:
    result = _run(open_url, "--max-commands", "2")
    assert result.returncode == 0, result.stderr
    assert "Sent 2 commands. Stopping." in result.stdout


def test_example_reports_a_missing_api_key(keyed_url: str) -> None:
    result = _run(keyed_url)
    assert result.returncode != 0
    assert "unauthorized" in result.stderr
