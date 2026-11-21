import base64
import hashlib
import re
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from bxapi.app import create_app
from bxapi.docs_pages import REDOC_SCRIPT, SWAGGER_UI_SCRIPT, SWAGGER_UI_STYLE, PinnedFile
from bxapi.settings import Settings

PINNED = [SWAGGER_UI_SCRIPT, SWAGGER_UI_STYLE, REDOC_SCRIPT]


def _directives(policy: str) -> dict[str, list[str]]:
    """Return a Content-Security-Policy as a mapping from each directive to its sources."""
    parts = [directive.split() for directive in policy.split(";")]
    return {name: sources for name, *sources in parts}


@pytest.mark.parametrize("file", PINNED)
def test_each_file_is_one_exact_version_with_a_sha384_hash(file: PinnedFile) -> None:
    assert re.fullmatch(r"https://cdn\.jsdelivr\.net/npm/[a-z-]+@\d+\.\d+\.\d+/[A-Za-z0-9./-]+\.(js|css)", file.url)
    algorithm, _, digest = file.integrity.partition("-")
    assert algorithm == "sha384"
    assert len(base64.b64decode(digest, validate=True)) == 48


def test_the_swagger_ui_page_loads_only_its_pinned_files_and_its_start_script(client: TestClient) -> None:
    page = client.get("/docs")
    assert page.status_code == 200
    tags = re.findall(r'<(?:script|link rel="stylesheet")[^>]*(?:src|href)="([^"]+)"([^>]*)>', page.text)
    assert [url for url, _ in tags] == [SWAGGER_UI_STYLE.url, SWAGGER_UI_SCRIPT.url]
    for (_, attributes), file in zip(tags, [SWAGGER_UI_STYLE, SWAGGER_UI_SCRIPT], strict=True):
        assert f'integrity="{file.integrity}"' in attributes
        assert 'crossorigin="anonymous"' in attributes
    [start] = re.findall(r"<script>(.*?)</script>", page.text, flags=re.DOTALL)
    assert 'url: "/openapi.json"' in start
    start_hash = base64.b64encode(hashlib.sha256(start.encode()).digest()).decode()
    assert _directives(page.headers["content-security-policy"]) == {
        "default-src": ["'none'"],
        "connect-src": ["'self'"],
        "img-src": ["'self'", "data:"],
        "base-uri": ["'none'"],
        "frame-ancestors": ["'none'"],
        "script-src": [SWAGGER_UI_SCRIPT.url, f"'sha256-{start_hash}'"],
        "style-src": [SWAGGER_UI_STYLE.url],
    }


def test_the_redoc_page_loads_only_its_pinned_script(client: TestClient) -> None:
    page = client.get("/redoc")
    assert page.status_code == 200
    assert re.findall(r'<script[^>]*src="([^"]+)"', page.text) == [REDOC_SCRIPT.url]
    assert f'integrity="{REDOC_SCRIPT.integrity}"' in page.text
    assert '<redoc spec-url="/openapi.json">' in page.text
    assert "<script>" not in page.text
    assert _directives(page.headers["content-security-policy"]) == {
        "default-src": ["'none'"],
        "connect-src": ["'self'"],
        "img-src": ["'self'", "data:"],
        "base-uri": ["'none'"],
        "frame-ancestors": ["'none'"],
        "script-src": [REDOC_SCRIPT.url],
        "style-src": ["'unsafe-inline'"],
        "worker-src": ["blob:"],
    }


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_a_page_asks_no_host_but_the_cdn_and_the_server(client: TestClient, path: str) -> None:
    page = client.get(path)
    hosts = set(re.findall(r"https?://([^/\"' ]+)", page.text + page.headers["content-security-policy"]))
    assert hosts == {"cdn.jsdelivr.net"}
    assert "unsafe-eval" not in page.headers["content-security-policy"]


def test_a_page_under_a_path_prefix_asks_for_the_document_under_it(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost", root_path="/game") as client:
        assert 'url: "/game/openapi.json"' in client.get("/docs").text
        assert '<redoc spec-url="/game/openapi.json">' in client.get("/redoc").text


def test_the_pages_are_not_in_the_openapi_document_and_have_no_oauth2_redirect(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/docs" not in paths and "/redoc" not in paths
    response = client.get("/docs/oauth2-redirect")
    assert (response.status_code, response.json()["code"]) == (404, "not_found")
    assert "oauth2" not in client.get("/docs").text


def test_docs_can_be_turned_off(settings: Settings, make_client: Callable[..., TestClient]) -> None:
    client = make_client(settings.model_copy(update={"docs_enabled": False}))
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = client.get(path)
        assert (response.status_code, response.json()["code"]) == (404, "not_found"), path
