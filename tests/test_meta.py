from fastapi.testclient import TestClient
from osrlib.versioning import SCHEMA_VERSION, engine_version

from bxapi import __version__


def test_health_needs_no_key(player: TestClient) -> None:
    response = player.get("/health", headers={"Authorization": ""})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_meta_reports_versions_and_local_referee(client: TestClient) -> None:
    body = client.get("/v1/meta").json()
    assert body == {
        "api_version": __version__,
        "engine_version": engine_version(),
        "schema_version": SCHEMA_VERSION,
        "auth_enabled": False,
        "client": "local",
        "role": "referee",
    }


def test_meta_reports_the_key_owner(player: TestClient, referee: TestClient, other: TestClient) -> None:
    assert player.get("/v1/meta").json() | {"api_version": ""} == {
        "api_version": "",
        "engine_version": engine_version(),
        "schema_version": SCHEMA_VERSION,
        "auth_enabled": True,
        "client": "game",
        "role": "player",
    }
    assert referee.get("/v1/meta").json()["role"] == "referee"
    assert other.get("/v1/meta").json()["client"] == "other"


def test_openapi_documents_the_bearer_scheme(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "bxapi"
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert "/v1/meta" in schema["paths"]
