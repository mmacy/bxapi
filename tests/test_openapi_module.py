"""`python -m bxapi.openapi` prints the application's OpenAPI document and touches no file."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bxapi.openapi import build_openapi, main


def test_main_prints_the_document(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    document = json.loads(capsys.readouterr().out)
    assert document["openapi"].startswith("3.")
    assert document["info"]["title"] == "bxapi"
    assert "/v1/sessions/{session_id}/commands" in document["paths"]
    operation = document["paths"]["/v1/dice/roll"]["post"]
    assert operation["operationId"] == "roll_dice"


def test_document_matches_the_servers(client, capsys: pytest.CaptureFixture[str]) -> None:
    main()
    assert json.loads(capsys.readouterr().out) == client.get("/openapi.json").json()


def test_build_openapi_ignores_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("BXAPI_PORT=not-a-number\n")
    monkeypatch.setenv("BXAPI_API_KEYS", "not json")
    monkeypatch.setenv("BXAPI_DOCS_ENABLED", "false")
    assert "/v1/meta" in build_openapi()["paths"]


def test_module_runs_and_creates_no_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("BXAPI_DATABASE=should-not-be-created.db\nBXAPI_PORT=not-a-number\n")
    before = sorted(path.name for path in tmp_path.iterdir())
    environment = {**os.environ, "BXAPI_DATABASE": str(tmp_path / "env.db"), "BXAPI_API_KEYS": "not json"}
    result = subprocess.run(
        [sys.executable, "-m", "bxapi.openapi"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["info"]["title"] == "bxapi"
    assert sorted(path.name for path in tmp_path.iterdir()) == before
