import re
from collections.abc import Callable

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from osrlib.versioning import SCHEMA_VERSION, engine_version
from pydantic import BaseModel

from bxapi import __version__
from bxapi.app import create_app
from bxapi.routers import V1_ROUTERS, meta
from bxapi.settings import Settings


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
        "storage": {"used_bytes": 0, "max_bytes": 1024**3},
    }


def test_meta_reports_the_key_owner(player: TestClient, referee: TestClient, other: TestClient) -> None:
    assert player.get("/v1/meta").json() | {"api_version": ""} == {
        "api_version": "",
        "engine_version": engine_version(),
        "schema_version": SCHEMA_VERSION,
        "auth_enabled": True,
        "client": "game",
        "role": "player",
        "storage": {"used_bytes": 0, "max_bytes": 1024**3},
    }
    assert referee.get("/v1/meta").json()["role"] == "referee"
    assert other.get("/v1/meta").json()["client"] == "other"


def test_openapi_documents_the_bearer_scheme(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "bxapi"
    assert "HTTPBearer" in schema["components"]["securitySchemes"]
    assert "/v1/meta" in schema["paths"]


def test_openapi_describes_errors_as_problem_documents(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/v1/meta"]["get"]["responses"]
    for status in ("401", "422"):
        assert list(responses[status]["content"]) == ["application/problem+json"]
        assert responses[status]["content"]["application/problem+json"]["schema"] == {
            "$ref": "#/components/schemas/Problem"
        }
    assert "application/json" in responses["200"]["content"]
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "Problem" in schema["components"]["schemas"]


def test_operation_ids_are_the_endpoint_names_and_unique(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation_ids = [operation["operationId"] for path in schema["paths"].values() for operation in path.values()]
    assert "get_meta" in operation_ids
    assert "get_health" in operation_ids
    assert len(operation_ids) == len(set(operation_ids))


def test_every_field_has_a_description(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert {"DiceRollRequest", "Meta", "MoveParty", "PlayerView", "Problem"} <= set(schemas)
    undescribed = [
        f"{name}.{field}"
        for name, schema in schemas.items()
        for field, field_schema in schema.get("properties", {}).items()
        if not field_schema.get("description")
    ]
    assert undescribed == []


def test_no_text_in_the_document_has_a_documentation_link(client: TestClient) -> None:
    # A link like [`MoveParty`][osrlib.crawl.commands.MoveParty] resolves only on osrlib's documentation site.
    text = client.get("/openapi.json").text
    assert re.findall(r"\[`[^`\]]+`\]\[[\w.]*\]", text) == []


def test_every_path_parameter_has_a_description(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    checked = 0
    for path, operations in paths.items():
        if not path.startswith("/v1"):
            continue
        for method, operation in operations.items():
            for parameter in operation.get("parameters", []):
                if parameter["in"] == "path":
                    checked += 1
                    assert parameter.get("description"), (
                        f"{method.upper()} {path}: {parameter['name']} has no description"
                    )
    assert checked >= 20


def test_problem_documents_its_extra_members(client: TestClient) -> None:
    problem = client.get("/openapi.json").json()["components"]["schemas"]["Problem"]
    assert problem["additionalProperties"] is True
    assert set(problem["required"]) == {"title", "status", "detail", "code"}
    for member in (
        "errors",
        "error_count",
        "resource",
        "id",
        "revision",
        "command_type",
        "field",
        "index",
        "rejections",
    ):
        assert member in problem["properties"]
        assert "default" not in problem["properties"][member]
    assert "docs/spec.md" not in problem["description"]


def test_openapi_documents_server_errors_and_body_limits(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    problem = {"$ref": "#/components/schemas/Problem"}
    for path, operations in schema["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for method, operation in operations.items():
            responses = operation["responses"]
            for status in ("401", "500", "503"):
                assert list(responses[status]["content"]) == ["application/problem+json"], f"{method} {path} {status}"
                assert responses[status]["content"]["application/problem+json"]["schema"] == problem, status
            has_body = "requestBody" in operation
            for status in ("400", "408", "413"):
                assert (status in responses) == has_body, f"{method} {path} {status}"
    assert schema["info"]["license"] == {"name": "MIT", "identifier": "MIT"}
    assert "ValidationError" not in schema["components"]["schemas"]


def test_openapi_keeps_a_route_s_own_error_schema(settings: Settings) -> None:
    class Teapot(BaseModel):
        reason: str

    app = create_app(settings)

    @app.get("/teapot", responses={418: {"model": Teapot}, 422: {"description": "Bad"}})
    def get_teapot() -> None:
        """Brew nothing."""

    responses = app.openapi()["paths"]["/teapot"]["get"]["responses"]
    assert responses["418"]["content"] == {"application/json": {"schema": {"$ref": "#/components/schemas/Teapot"}}}


def test_docs_can_be_turned_off(settings: Settings, make_client: Callable[..., TestClient]) -> None:
    shown = make_client(settings)
    hidden = make_client(settings.model_copy(update={"docs_enabled": False}))
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert shown.get(path).status_code == 200
        assert hidden.get(path).status_code == 404
    assert hidden.get("/health").status_code == 200


def test_docs_need_no_key(player: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert player.get(path, headers={"Authorization": ""}).status_code == 200


def _routes() -> list[tuple[str, str]]:
    """Return the method and path of every route, from the routers `create_app` mounts."""
    mounted = [("", meta.health_router)] + [("/v1", router) for router in V1_ROUTERS]
    return [
        (method, prefix + route.path)
        for prefix, router in mounted
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    ]


def test_every_v1_route_needs_a_key(keyed_settings: Settings, make_client: Callable[..., TestClient]) -> None:
    app = create_app(keyed_settings)
    anonymous = make_client(keyed_settings)
    checked = []
    for method, template in _routes():
        path = re.sub(r"\{[^}]+\}", "x", template)
        # A malformed body shows that the server checks the key before it reads the body.
        response = anonymous.request(method, path, content=b"{not json")
        if path.startswith("/v1/"):
            assert response.status_code == 401, f"{method} {template} returned {response.status_code}"
            assert response.json()["code"] == "unauthorized"
            checked.append(f"{method} {template}")
        else:
            assert response.status_code == 200, f"{method} {template} returned {response.status_code}"
    documented = [
        f"{method.upper()} {path}" for path, operations in app.openapi()["paths"].items() for method in operations
    ]
    assert sorted(checked) == sorted(entry for entry in documented if entry.split(" ")[1].startswith("/v1/"))
    assert "POST /v1/sessions/{session_id}/commands" in checked


def test_a_trailing_slash_is_not_redirected(client: TestClient) -> None:
    response = client.get("/v1/meta/", follow_redirects=False)
    assert (response.status_code, response.json()["code"]) == (404, "not_found")


def _union_members(union: dict[str, object]) -> tuple[str, list[str]]:
    discriminator = union["discriminator"]
    assert isinstance(discriminator, dict)
    return discriminator["propertyName"], sorted(set(discriminator["mapping"].values()))


def test_every_union_member_requires_its_tag(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    schemas = schema["components"]["schemas"]
    body = schema["paths"]["/v1/sessions/{session_id}/commands"]["post"]["requestBody"]["content"]["application/json"]
    events = schemas["CommandOutcome"]["properties"]["events"]["items"]
    for union, tag in ((body["schema"], "command_type"), (events, "event_type")):
        name, members = _union_members(union)
        assert name == tag
        assert len(members) > 10
        for ref in members:
            member = schemas[ref.removeprefix("#/components/schemas/")]
            assert tag in member["required"], f"{ref} doesn't require {tag}"
    assert set(schemas["MoveParty"]["required"]) == {"direction", "command_type"}


def test_a_command_without_its_tag_is_refused(client: TestClient) -> None:
    response = client.post("/v1/sessions/ses_x/commands", json={"direction": "north"})
    assert (response.status_code, response.json()["code"]) == (422, "validation_error")
