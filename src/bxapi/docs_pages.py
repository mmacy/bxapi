"""The interactive documentation pages at `/docs` and `/redoc`.

`/docs` shows the OpenAPI document with [Swagger UI](https://github.com/swagger-api/swagger-ui),
and `/redoc` shows it with [ReDoc](https://github.com/Redocly/redoc). Each page loads its script
from the jsDelivr content delivery network (CDN), which serves the files of npm packages.

A person who uses `/docs` types an API key into the page, so a changed script could send the key
anywhere. Two things stop that:

- Each `script` and `link` tag names one exact version of the file and has an `integrity`
  attribute, the hash of that file. The browser refuses a file with another hash, so the CDN
  can't serve the page a changed script.
- Each page has a `Content-Security-Policy` header that lists what the page may load: the pinned
  files, its own start script, and requests to the server itself. A script on the page can't send
  anything to another host.

To move to a newer Swagger UI or ReDoc, change the version in the constants below and compute
each file's hash again:

```sh
URL=https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.34.1/swagger-ui-bundle.js
curl -s "$URL" | openssl dgst -sha384 -binary | openssl base64 -A
```

Then load both pages in a browser and check that the console reports no blocked request.
"""

import base64
import hashlib
import json
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

__all__ = ["REDOC_SCRIPT", "SWAGGER_UI_SCRIPT", "SWAGGER_UI_STYLE", "PinnedFile", "install_docs_pages"]

_CDN = "https://cdn.jsdelivr.net/npm"


@dataclass(frozen=True, slots=True)
class PinnedFile:
    """One file on the CDN, at an exact version, with the hash the browser checks it against."""

    url: str
    """The file's URL, with the package's exact version."""
    integrity: str
    """The file's Subresource Integrity value: `sha384-` and the Base64 SHA-384 hash of the file."""


SWAGGER_UI_SCRIPT = PinnedFile(
    f"{_CDN}/swagger-ui-dist@5.34.1/swagger-ui-bundle.js",
    "sha384-XFYN47VzPiDY6qPtxYKg0nqUeW/upo20xf1j2SUE7gT/i35FJB4cuS/71Iegzk8Z",
)
"""The script of the Swagger UI page."""

SWAGGER_UI_STYLE = PinnedFile(
    f"{_CDN}/swagger-ui-dist@5.34.1/swagger-ui.css",
    "sha384-4c3vsE5f+trhFyHrFrLM1fFGUn531NIaC7dcqEWIJdoRQJ5CkJbSuSCwXLpaS1KF",
)
"""The style sheet of the Swagger UI page."""

REDOC_SCRIPT = PinnedFile(
    f"{_CDN}/redoc@2.5.4/bundles/redoc.standalone.js",
    "sha384-w447zOpYfw/1Tv/5AK9NfHTlQIqE3RVR6KY62jCyy9zNDgO64cMwGGP1Fj0zJVf5",
)
"""The script of the ReDoc page."""

# What every page's policy has: nothing loads unless a line below allows it, the page calls only
# the server it came from, and no page can show it in a frame.
_BASE_POLICY = "default-src 'none'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"

# Each closing `"""` below is on the last line of its text, because pytest-examples reads a line that starts with
# three quotes as the start of a docstring, and then finds no example in the docstrings after it.
_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">\n"""


def _script_string(value: str) -> str:
    # JSON is a JavaScript string. The escape keeps a "</script>" in the value from ending the script tag.
    return json.dumps(value).replace("<", "\\u003c")


def _attribute(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _hash_source(script: str) -> str:
    digest = hashlib.sha256(script.encode()).digest()
    return f"'sha256-{base64.b64encode(digest).decode()}'"


def _swagger_ui_page(openapi_url: str) -> HTMLResponse:
    start = f"""
window.ui = SwaggerUIBundle({{
  url: {_script_string(openapi_url)},
  dom_id: "#swagger-ui",
  layout: "BaseLayout",
  deepLinking: true,
  showExtensions: true,
  showCommonExtensions: true,
  validatorUrl: null,
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
}});\n"""
    html = f"""{_HEAD}<title>bxapi: Swagger UI</title>
<link rel="stylesheet" href="{SWAGGER_UI_STYLE.url}" integrity="{SWAGGER_UI_STYLE.integrity}" crossorigin="anonymous">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{SWAGGER_UI_SCRIPT.url}" integrity="{SWAGGER_UI_SCRIPT.integrity}" crossorigin="anonymous"></script>
<script>{start}</script>
</body>
</html>\n"""
    policy = (
        f"{_BASE_POLICY}; script-src {SWAGGER_UI_SCRIPT.url} {_hash_source(start)}; style-src {SWAGGER_UI_STYLE.url}"
    )
    return HTMLResponse(html, headers={"Content-Security-Policy": policy})


def _redoc_page(openapi_url: str) -> HTMLResponse:
    html = f"""{_HEAD}<title>bxapi: ReDoc</title>
<style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
<redoc spec-url="{_attribute(openapi_url)}"></redoc>
<script src="{REDOC_SCRIPT.url}" integrity="{REDOC_SCRIPT.integrity}" crossorigin="anonymous"></script>
</body>
</html>\n"""
    # ReDoc writes its styles into the page as it draws, and runs its search in a worker that it starts from a blob.
    # The policy blocks the logo that ReDoc's footer loads from cdn.redoc.ly, so the footer shows its text alone.
    policy = f"{_BASE_POLICY}; script-src {REDOC_SCRIPT.url}; style-src 'unsafe-inline'; worker-src blob:"
    return HTMLResponse(html, headers={"Content-Security-Policy": policy})


def install_docs_pages(app: FastAPI, *, openapi_url: str) -> None:
    """Add the `/docs` and `/redoc` pages to an application.

    The pages replace the ones FastAPI serves, which load whatever file the CDN has for a major
    version, with no hash and no `Content-Security-Policy`. The OpenAPI document doesn't list
    the two routes.

    Args:
        app: The application to add the routes to. Build it with `docs_url=None` and
            `redoc_url=None`.
        openapi_url: The path of the application's OpenAPI document, like `"/openapi.json"`.

    Examples:
        ```python
        from fastapi.testclient import TestClient

        from bxapi.app import create_app
        from bxapi.docs_pages import SWAGGER_UI_SCRIPT
        from bxapi.settings import Settings

        client = TestClient(create_app(Settings(database=":memory:", allow_anonymous=True)))
        page = client.get("/docs")
        assert SWAGGER_UI_SCRIPT.integrity in page.text
        assert SWAGGER_UI_SCRIPT.url in page.headers["Content-Security-Policy"]
        ```
    """

    def document_url(request: Request) -> str:
        # Behind a proxy that serves the API under a path prefix, the browser needs the prefix.
        return request.scope.get("root_path", "").rstrip("/") + openapi_url

    @app.get("/docs", include_in_schema=False)
    def swagger_ui(request: Request) -> HTMLResponse:
        return _swagger_ui_page(document_url(request))

    @app.get("/redoc", include_in_schema=False)
    def redoc(request: Request) -> HTMLResponse:
        return _redoc_page(document_url(request))
