# AGENTS.md

Guidance for agents working on bxapi, an HTTP API for the 1981 B/X rules. Other applications use it as their backend.

## Architecture in one paragraph

[osrlib](https://github.com/mmacy/osrlib-python) applies every game rule. bxapi is a FastAPI application that puts osrlib behind HTTP and adds authentication, storage, and one-at-a-time access to each game. `src/bxapi/app.py` builds the application. `settings.py` reads the `BXAPI_` environment variables, `auth.py` turns an API key into a client and a role, `errors.py` turns every failure into a problem document, and `db.py` is the only module that runs SQL. Each group of endpoints is one module in `src/bxapi/routers/`. `docs/spec.md` is the API's contract: read it before you add or change an endpoint, and change it in the same pull request when the API changes.

osrlib is installed from PyPI, and the installed source in `.venv/lib/python3.14/site-packages/osrlib/` is the authority for every name, field, and behavior. Its documentation is at <https://mmacy.github.io/osrlib-python/>. A checkout of osrlib on the same machine, like `~/repos/osr-suite/osrlib-python`, can be many commits ahead of the release bxapi installs, so confirm anything you read there against the installed package, and never edit the checkout.

## Run and verify

```sh
uv sync
BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

The server listens on <http://127.0.0.1:8640>, with interactive documentation at `/docs`. With no `BXAPI_API_KEYS` set, it starts only when `BXAPI_ALLOW_ANONYMOUS` is `true`, and then asks for no key. To run it with keys, set `BXAPI_API_KEYS` as `docs/spec.md` describes under "Settings".

To check a change from outside the code, start the server on a spare port with a temporary database and drive it with `curl` or a short script:

```sh
BXAPI_PORT=8641 BXAPI_DATABASE=/tmp/bxapi-check.db BXAPI_ALLOW_ANONYMOUS=true uv run bxapi
```

If you start a server, record its process id and stop that id alone. Never stop a process by name.

## The gate

Every pull request passes the commands continuous integration runs:

```sh
uv sync --locked
uv run ruff format --check
uv run ruff check
uv run pyright
uv run pytest
```

Fix a finding in the code. Don't add `noqa`, `type: ignore`, or a rule to an ignore list. `pyproject.toml` has two ignores: ruff's `D107`, because a class docstring documents the constructor's arguments, and the docstring rules (`D`) for `tests/`. The one allowed suppression in the code is `# pyright: ignore[reportCallIssue]` on a call to the `Settings` constructor in a test that passes `_env_file`, which pyright can't see.

The CI workflow runs a second job that installs the lowest version of each direct dependency that `pyproject.toml` allows and runs `pytest`. When you raise a lower bound or add a dependency, run the same check:

```sh
uv sync --resolution lowest-direct
uv run --no-sync pytest
```

The sync rewrites `uv.lock`, so run it in a copy of the worktree, or restore `uv.lock` and run `uv sync` afterward.

`tests/test_docstring_examples.py` lints and runs every fenced `python` block in the docstrings under `src/bxapi/`.

## Rules for the code

- **Never implement a rule.** If an endpoint needs a number, a check, or a roll, osrlib has a function for it. If osrlib doesn't, the endpoint doesn't belong in bxapi yet.
- **Return osrlib's models unchanged** wherever one exists. Don't rename fields or build a parallel model of something osrlib already serializes.
- **Player visibility by default.** Before you add a response field, ask whether it shows a player something the referee hides. `docs/spec.md` lists what only a referee may read.
- **Filter every query on the caller's client.** A resource that belongs to another client is a 404.
- **Raise `ApiError`** (or use the helpers in `bxapi.errors`) for every failure. Never return an error as a plain dict or raise FastAPI's `HTTPException`. Add a new error code to the table in `docs/spec.md`.
- **Endpoints are plain `def`.** No `async def` endpoint, and no hidden global state. Everything an endpoint needs comes from `request.app.state` or a dependency.
- **Every route has a `summary`, a docstring, and typed request and response models.** They become the OpenAPI document, which is the reference clients read. A response model that bxapi defines extends `bxapi.models.ApiModel`, and a request body model extends `bxapi.models.RequestModel`, which also refuses unknown fields. Document each field with a docstring on the line after it: both base classes copy it into the OpenAPI document. The endpoint function's name becomes the operation's `operationId`, so give it a name that reads well as a client method (`roll_dice`, `get_session`), keep it unique across the application, and don't rename it once it's merged.
- **Tests go through HTTP.** Test an endpoint with the `client`, `player`, `referee`, and `other` fixtures in `tests/conftest.py`. Assert on structure and on what osrlib returned, and never on a particular die roll unless the test passes a seed.
- Refactor freely and update every call site. Keep no alias for an old name and no code "just in case".

## Python style

- Python 3.14. Use built-in generic types (`list[str]`, `dict[str, int]`), and never `from __future__ import annotations`.
- Follow the Google Python Style Guide. `ruff` enforces a 120-column line.
- Write docstrings in Markdown, in Google style, with `Args:`, `Returns:`, `Raises:`, and `Examples:` sections where they apply. Link to other code as ``[`Name`][package.module.Name]``. An example in a docstring must run as written, and the gate runs it.
- Each module defines `__all__`.

## Writing

These rules apply to docstrings, comments, Markdown files, OpenAPI descriptions, commit messages, and pull request descriptions.

- Plain declarative sentences, one idea each, in the present tense and active voice. Address the reader as *you*. Use contractions.
- No em dashes or en dashes. No semicolons in prose. Sentence-case headings. Never hard-wrap a Markdown paragraph.
- State facts, and leave judgments like *simple*, *powerful*, or *robust* out.
- Name who acts. Software can do what it does (the server returns, osrlib rolls). A file or an object doesn't *want*, *know*, *own*, *carry*, or *hold* anything.
- Define a term the first time you use it.
- Never open with a sentence about the document (*This module provides ...*), and never end with a summary.
- Read `~/.claude/skills/clauterize/references/tells.md` before you write prose, if the file exists. It lists the habits and stock words to avoid, with before-and-after examples.

## Pull requests

- Cut every branch from `origin/main`, and work in the worktree you were given. Never check out `main` in a worktree.
- Before you open a pull request, run `git log --oneline origin/main..HEAD` and confirm that it lists only your commits.
- A pull request description is short and in the first person: what changed, why, and what a reviewer should check. No headings unless the change has several parts.
- A commit message says what changed and why, at a length that fits the change.
