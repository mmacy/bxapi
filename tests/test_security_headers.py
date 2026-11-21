from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from bxapi.app import create_app
from bxapi.security_headers import API_HEADERS, CONTENT_SECURITY_POLICY, PAGE_HEADERS
from bxapi.settings import Settings

from .conftest import LOCAL_URL, PLAYER_KEY, bearer


def test_the_headers_tell_a_browser_to_run_and_frame_nothing() -> None:
    assert PAGE_HEADERS == {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }
    assert CONTENT_SECURITY_POLICY == "default-src 'none'; frame-ancestors 'none'"
    assert API_HEADERS == PAGE_HEADERS | {"Content-Security-Policy": CONTENT_SECURITY_POLICY}


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/health", 200),
        ("HEAD", "/health", 200),
        ("GET", "/openapi.json", 200),
        ("GET", "/v1/meta", 200),
        ("POST", "/v1/dice/roll", 422),
        ("GET", "/v1/sessions/ses_none", 404),
        ("GET", "/nowhere", 404),
        ("DELETE", "/health", 405),
    ],
)
def test_every_api_response_has_the_headers(player: TestClient, method: str, path: str, status: int) -> None:
    response = player.request(method, path)
    assert response.status_code == status
    for name, value in API_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


def test_the_errors_the_middleware_sends_have_the_headers(
    settings: Settings, keyed_settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    allowed = "https://game.example"
    keyed = make_client(keyed_settings.model_copy(update={"cors_origins": (allowed,), "max_body_bytes": 1024}))
    preflight = {"Origin": allowed, "Access-Control-Request-Method": "POST"}
    etag = keyed.get("/health").headers["etag"]
    sent = {
        401: keyed.get("/v1/meta"),
        413: keyed.post("/v1/dice/roll", content=b" " * 1025, headers=bearer(PLAYER_KEY)),
        304: keyed.get("/health", headers={"If-None-Match": etag}),
        200: keyed.options("/v1/dice/roll", headers=preflight),
        421: make_client(settings).get("/v1/meta", headers={"Host": "game.example"}),
    }
    for status, response in sent.items():
        assert response.status_code == status
        for name, value in API_HEADERS.items():
            assert response.headers.get_list(name) == [value], (status, name)


def test_an_unexpected_error_has_the_headers(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/v1/bug")
    def bug() -> None:
        raise RuntimeError("SECRET")

    with TestClient(app, base_url=LOCAL_URL, raise_server_exceptions=False) as client:
        response = client.get("/v1/bug")
    assert response.status_code == 500
    for name, value in API_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_a_documentation_page_can_load_its_scripts_and_cannot_be_framed(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for name, value in PAGE_HEADERS.items():
        assert response.headers.get_list(name) == [value], name
    assert "content-security-policy" not in response.headers


def test_the_server_has_no_oauth2_redirect_page(client: TestClient) -> None:
    response = client.get("/docs/oauth2-redirect")
    assert (response.status_code, response.json()["code"]) == (404, "not_found")
    assert "oauth2-redirect" not in client.get("/docs").text
