# AGENTS.md

bxapi is an HTTP API for the 1981 B/X rules. Other applications use it as their backend.

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

The CI workflow's `docs` job builds the documentation site. When you change a hand-written page, a route, a model, or an example, build the site as "The documentation site" describes.

## The documentation site

`website/` is a [Docusaurus](https://docusaurus.io/) site with two parts. The guides are the hand-written pages: `README.md`, every file in `docs/` except `docs/spec.md`, and `examples/README.md`. The API reference is a set of pages that [Docs MD](https://github.com/speakeasy-api/docs-md), Speakeasy's generator of reference pages, writes from the OpenAPI document. On a push to `main` that passes the gate, CI publishes the site to GitHub Pages at <https://mmacy.github.io/bxapi/>.

Build the site and serve it at <http://localhost:3000/bxapi/>. You need [Node.js](https://nodejs.org/) 22 or later:

```sh
cd website
npm ci
npm run build
npm run serve
```

`npm run build` first runs `npm run generate`, which writes everything under `website/generated/`, a directory that git ignores:

- `scripts/stage-pages.mjs` copies the hand-written pages to `generated/content/`.
- `scripts/prepare-openapi.mjs` runs `uv run python -m bxapi.openapi` and writes the document to `generated/openapi.json`. It also writes one curl command for each operation to `generated/curl-samples.json`. The command is the request of the operation's first successful exchange in `src/bxapi/openapi_examples.json`.
- `docs-md` reads `speakeasy.config.mjs` and writes the reference pages to `generated/content/reference/`.

`npm start` serves the site and reloads a page when its file under `generated/` changes. After you edit a hand-written page, run `node scripts/stage-pages.mjs` in a second terminal to copy it again.

- Edit a hand-written page where git tracks it, never under `website/generated/`.
- Link from one hand-written page to another with a relative path to the file, like `../README.md#get-it-running`. The link then works on GitHub and on the site. The build fails on a broken link or anchor.
- When you add a file to `docs/`, add it to `website/sidebars.js` and to the list in `README.md`.
- To change the text of the reference, change the route's summary, docstring, models, error entries, or examples in `src/bxapi/`. Docs MD copies them from the OpenAPI document.
- `package.json` pins `@speakeasy-api/docs-md` to one version, because four parts of the build work around defects in 0.2.37. `inlineRefs` in `scripts/prepare-openapi.mjs` exists because Docs MD stops with an error on a `$ref` key in an example. `separateSchema` in the same script exists because Docs MD shows the first response's examples on every response of an operation that has the same schema, and leaves some of those responses out. `src/remark/unescape-code-braces.mjs` removes the backslash that Docs MD writes before each brace in code. `src/remark/curl-samples.mjs` replaces the curl samples that Docs MD makes up from the schemas. When you move to a newer version, remove each workaround the new version doesn't need.

## Rules for the code

- **Never implement a rule.** If an endpoint needs a number, a check, or a roll, osrlib has a function for it. If osrlib doesn't, the endpoint doesn't belong in bxapi yet.
- **Return osrlib's models unchanged** wherever one exists. Don't rename fields or build a parallel model of something osrlib already serializes.
- **Player visibility by default.** Before you add a response field, ask whether it shows a player something the referee hides. `docs/spec.md` lists what only a referee may read.
- **Filter every query on the caller's client.** A resource that belongs to another client is a 404.
- **Raise `ApiError`** (or use the helpers in `bxapi.errors`) for every failure. Never return an error as a plain dict or raise FastAPI's `HTTPException`. Add a new error code to the table in `docs/spec.md`.
- **Endpoints are plain `def`.** No `async def` endpoint, and no hidden global state. Everything an endpoint needs comes from `request.app.state` or a dependency.
- **Every route has a `summary`, a docstring, and typed request and response models.** They become the OpenAPI document, which is the reference clients read. A response model that bxapi defines extends `bxapi.models.ApiModel`, and a request body model extends `bxapi.models.RequestModel`, which also forbids unknown fields. Document each field with a docstring on the line after it. With either base class, that docstring becomes the field's description in the OpenAPI document. The endpoint function's name becomes the operation's `operationId`, so give it a name that reads well as a client method (`roll_dice`, `get_session`), keep it unique across the application, and don't rename it once it's merged. A route's docstring becomes the operation's description, which a client developer reads as Markdown, so write it for that reader and leave `Args:` and `Returns:` sections out of it.
- **Every route documents its errors and has a tested example.** Pass `problem_responses` one entry for each error code the route returns: the code, what causes it, and what the caller does about it. Add at least one exchange for the route to `src/bxapi/openapi_examples.json`, and one for each error code the route lists, then record the responses with `uv run python -m tests.test_openapi_examples`. An error that reads the same on every route, or that a request alone can't cause, like 503 `database_busy`, gets one example in `shared_problems` in `src/bxapi/openapi_examples.py` instead, built with the function the server calls to report it. `tests/test_openapi_examples.py` fails when a route has no example, when an error code a route lists has no example, and when the server's response differs from the recorded one. When a change alters a response, record the examples again and read the difference before you commit it.
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
