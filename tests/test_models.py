import pytest
from pydantic import ValidationError

from bxapi.models import ApiModel, RequestModel
from bxapi.pagination import Page


class _Rename(RequestModel):
    name: str
    """The new name."""


class _Info(ApiModel):
    name: str
    """The current name."""


def test_request_models_refuse_unknown_fields() -> None:
    assert _Rename.model_validate({"name": "Second try"}).name == "Second try"
    with pytest.raises(ValidationError, match="nmae"):
        _Rename.model_validate({"name": "Second try", "nmae": "typo"})


def test_field_docstrings_become_schema_descriptions() -> None:
    assert _Rename.model_json_schema()["properties"]["name"]["description"] == "The new name."
    assert _Info.model_json_schema()["properties"]["name"]["description"] == "The current name."
    page = Page[_Info].model_json_schema()["properties"]
    assert all(page[field].get("description") for field in ("items", "total", "limit", "offset"))


def test_response_models_ignore_unknown_fields() -> None:
    assert _Info.model_validate({"name": "x", "extra": 1}).name == "x"
