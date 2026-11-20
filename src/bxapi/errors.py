"""The error format the API returns, and the exceptions that produce it.

Every error response is a problem document as RFC 9457 defines it, sent as
`application/problem+json`. A client reads `code` to decide what to do and shows `detail` to
a person:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "No session has the id 'ses_x'.",
  "code": "not_found",
  "resource": "session",
  "id": "ses_x"
}
```

An endpoint raises [`ApiError`][bxapi.errors.ApiError], or calls one of the functions here
that build one, and [`install_error_handlers`][bxapi.errors.install_error_handlers] turns the
exception into the response. The same handlers convert a request that fails validation, an
osrlib exception, and any exception the code didn't expect.

A command the rules refuse isn't an error. The command endpoint returns it with status 200
and `accepted` set to `false`.
"""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from osrlib.errors import ContentValidationError, ReplayVersionError, SaveVersionError
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

__all__ = [
    "PROBLEM_MEDIA_TYPE",
    "ApiError",
    "Problem",
    "conflict",
    "forbidden",
    "install_error_handlers",
    "invalid",
    "not_found",
    "problem_response",
    "problem_responses",
]

PROBLEM_MEDIA_TYPE = "application/problem+json"
"""The media type of every error response."""

_log = logging.getLogger("bxapi")


class Problem(BaseModel):
    """The body of an error response.

    Some codes add extra members, like `resource` and `id` on `not_found` or `errors` on
    `validation_error`. The table of codes in `docs/spec.md` lists them.
    """

    model_config = ConfigDict(extra="allow")

    type: str = "about:blank"
    """Always `"about:blank"`. Read `code` to identify the error."""
    title: str
    """The standard phrase for the HTTP status, like `"Not Found"`."""
    status: int
    """The HTTP status code, repeated from the response."""
    detail: str
    """One or two sentences that say what went wrong, written for a person."""
    code: str
    """A fixed snake_case name for the error, like `"not_found"`. It doesn't change between
    releases, so a client can branch on it."""


class ApiError(Exception):
    """An error the API reports to the caller as a problem document.

    Raise it from an endpoint, or from code an endpoint calls, when the request can't be
    carried out. The handler that
    [`install_error_handlers`][bxapi.errors.install_error_handlers] registers sends it as a
    [`Problem`][bxapi.errors.Problem].

    Args:
        status: The HTTP status code of the response.
        code: The fixed snake_case name of the error.
        detail: What went wrong, written for a person.
        headers: Extra response headers, like `WWW-Authenticate` on a 401.
        **extras: Extra members of the problem document, like `resource="session"`.

    Examples:
        ```python
        from bxapi.errors import ApiError

        error = ApiError(409, "revision_conflict", "The session has moved on.", revision=12)
        assert error.problem().model_dump()["revision"] == 12
        ```
    """

    def __init__(
        self, status: int, code: str, detail: str, *, headers: dict[str, str] | None = None, **extras: Any
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers
        self.extras = extras

    def problem(self) -> Problem:
        """Return the problem document for this error."""
        return Problem(
            title=HTTPStatus(self.status).phrase, status=self.status, detail=self.detail, code=self.code, **self.extras
        )


def not_found(resource: str, resource_id: str) -> ApiError:
    """Build the 404 for an identifier that matches nothing the caller can read.

    Use it for a resource that belongs to another client too, so a caller can't learn that the
    identifier exists.

    Args:
        resource: The kind of resource, like `"session"`.
        resource_id: The identifier the caller sent.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `not_found`.
    """
    return ApiError(404, "not_found", f"No {resource} has the id {resource_id!r}.", resource=resource, id=resource_id)


def forbidden(code: str, detail: str, **extras: Any) -> ApiError:
    """Build the 403 for a caller whose role doesn't allow the request.

    Args:
        code: The error code, like `"referee_required"`.
        detail: What the caller may not do.
        **extras: Extra members of the problem document.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with status 403.
    """
    return ApiError(403, code, detail, **extras)


def conflict(code: str, detail: str, **extras: Any) -> ApiError:
    """Build the 409 for a request that conflicts with the state of a resource.

    Args:
        code: The error code, like `"revision_conflict"`.
        detail: What the request conflicts with.
        **extras: Extra members of the problem document.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with status 409.
    """
    return ApiError(409, code, detail, **extras)


def invalid(code: str, detail: str, **extras: Any) -> ApiError:
    """Build the 422 for a request that's well formed but that the server can't accept.

    Args:
        code: The error code, like `"character_invalid"`.
        detail: What's wrong with the request.
        **extras: Extra members of the problem document, like a list of rejections.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with status 422.
    """
    return ApiError(422, code, detail, **extras)


def problem_response(problem: Problem, headers: dict[str, str] | None = None) -> JSONResponse:
    """Return the HTTP response for a problem document.

    Args:
        problem: The problem to send.
        headers: Extra response headers.

    Returns:
        A response with the problem's status and the `application/problem+json` media type.
    """
    return JSONResponse(
        status_code=problem.status,
        content=jsonable_encoder(problem.model_dump()),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Return the `responses` argument that documents an endpoint's error statuses in OpenAPI.

    Pass the result to a route decorator. Every route under `/v1` can also return 401 and 422,
    which the router documents once for all of them. [`create_app`][bxapi.app.create_app]
    changes the media type of each of these responses to `application/problem+json` when it
    builds the OpenAPI document.

    Args:
        *statuses: The error statuses the endpoint can return, like `404` and `409`.

    Returns:
        A mapping from each status to its OpenAPI response description.

    Examples:
        ```python
        from bxapi.errors import problem_responses

        responses = problem_responses(404)
        assert responses[404]["description"] == "Not Found"
        ```
    """
    return {status: {"description": HTTPStatus(status).phrase, "model": Problem} for status in statuses}


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that turn exceptions into problem documents.

    [`create_app`][bxapi.app.create_app] calls it once. The handlers cover:

    - [`ApiError`][bxapi.errors.ApiError], sent as raised.
    - A request FastAPI can't validate, sent as 422 `validation_error` with pydantic's list of
      errors in `errors`.
    - osrlib's `ContentValidationError`, sent as 422 `content_invalid`.
    - osrlib's `SaveVersionError` and `ReplayVersionError`, sent as 409
      `version_unsupported`.
    - Starlette's `HTTPException`, like the 404 for a path with no route.
    - Any other exception, logged with its traceback and sent as 500 `internal_error` with no
      detail about the cause.

    Args:
        app: The application to register the handlers on.
    """

    @app.exception_handler(ApiError)
    def _api_error(request: Request, error: ApiError) -> JSONResponse:
        return problem_response(error.problem(), error.headers)

    @app.exception_handler(RequestValidationError)
    def _request_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(entry["loc"]), "msg": entry["msg"], "type": entry["type"]} for entry in error.errors()]
        problem = ApiError(
            422, "validation_error", "The request failed validation. See `errors` for each field.", errors=errors
        ).problem()
        return problem_response(problem)

    @app.exception_handler(ContentValidationError)
    def _content_validation_error(request: Request, error: ContentValidationError) -> JSONResponse:
        return problem_response(ApiError(422, "content_invalid", str(error)).problem())

    @app.exception_handler(SaveVersionError)
    @app.exception_handler(ReplayVersionError)
    def _version_error(request: Request, error: Exception) -> JSONResponse:
        return problem_response(ApiError(409, "version_unsupported", str(error)).problem())

    @app.exception_handler(StarletteHTTPException)
    def _http_exception(request: Request, error: StarletteHTTPException) -> JSONResponse:
        phrase = HTTPStatus(error.status_code).phrase
        code = phrase.lower().replace(" ", "_").replace("-", "_")
        detail = error.detail if isinstance(error.detail, str) else phrase
        problem = Problem(title=phrase, status=error.status_code, detail=detail, code=code)
        return problem_response(problem, dict(error.headers) if error.headers else None)

    @app.exception_handler(Exception)
    def _unexpected(request: Request, error: Exception) -> JSONResponse:
        _log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=error)
        return problem_response(ApiError(500, "internal_error", "The server failed to handle the request.").problem())
