"""Every fenced `python` block in the docstrings under `src/bxapi/` lints clean and runs as written."""

from pathlib import Path

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

_SOURCE = Path(__file__).resolve().parent.parent / "src" / "bxapi"


@pytest.mark.parametrize("example", [pytest.param(example, id=str(example)) for example in find_examples(_SOURCE)])
def test_docstring_example(example: CodeExample, eval_example: EvalExample, monkeypatch: pytest.MonkeyPatch) -> None:
    eval_example.set_config(
        line_length=120,
        ruff_line_length=120,
        ruff_select=["B", "E", "F", "I", "UP", "W"],
        # The repository's pydocstyle rules are for modules, not for examples.
        ruff_ignore=["D"],
    )
    eval_example.lint_ruff(example)
    # An example that writes a file writes it in the test's temporary directory.
    monkeypatch.chdir(eval_example.tmp_path)
    eval_example.run(example)
