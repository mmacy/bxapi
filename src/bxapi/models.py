"""The base classes for the models the API defines."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.config import JsonDict
from pydantic_core import PydanticCustomError

__all__ = ["ApiModel", "RequestModel", "left_out_when_none"]

_MAX_UNKNOWN_FIELDS = 20


def _leave_out_default(schema: JsonDict) -> None:
    schema.pop("default", None)


def _is_none(value: object) -> bool:
    return value is None


def left_out_when_none() -> Any:
    """Return the definition of a response field that the server leaves out of the JSON instead of sending `null`.

    Annotate the field as `SomeType | SkipJsonSchema[None]`, so its schema in the OpenAPI document
    is `SomeType`, with no `null` and no default.

    Returns:
        A pydantic field with the default `None`.

    Examples:
        ```python
        from pydantic import BaseModel
        from pydantic.json_schema import SkipJsonSchema

        from bxapi.models import left_out_when_none


        class Outcome(BaseModel):
            revision: int
            note: str | SkipJsonSchema[None] = left_out_when_none()


        assert Outcome(revision=1).model_dump() == {"revision": 1}
        assert Outcome(revision=1, note="saved").model_dump() == {"revision": 1, "note": "saved"}
        assert Outcome.model_json_schema()["properties"]["note"] == {"title": "Note", "type": "string"}
        ```
    """
    return Field(default=None, exclude_if=_is_none, json_schema_extra=_leave_out_default)


class ApiModel(BaseModel):
    """The base class of every response model that bxapi defines.

    Document each field with a docstring on the line after it. pydantic copies that docstring
    into the field's description, and FastAPI copies the description into the OpenAPI document,
    which is the reference a client developer reads.

    A response model from osrlib doesn't extend this class. The server returns it as osrlib
    defines it.

    Examples:
        ```python
        from bxapi.models import ApiModel


        class Health(ApiModel):
            status: str
            "The state of the server."


        assert Health.model_json_schema()["properties"]["status"]["description"] == "The state of the server."
        ```
    """

    model_config = ConfigDict(use_attribute_docstrings=True)


class RequestModel(ApiModel):
    """The base class of every request body that bxapi defines.

    A request with a field the model doesn't define gets 422 `validation_error`. Without that
    check, pydantic would drop a misspelled field name, and the server would act on the default.
    The response lists each unknown field, up to 20 of them. An object with more than 20
    unknown fields gets one error for the object instead, because FastAPI copies every error
    pydantic reports before the server can cut the list short.

    A body that is an osrlib model, like a command, follows osrlib's rules instead. osrlib
    ignores unknown fields, so that a client written for a newer osrlib still works.

    Examples:
        ```python
        import pytest
        from pydantic import ValidationError

        from bxapi.models import RequestModel


        class Rename(RequestModel):
            name: str


        assert Rename.model_validate({"name": "Second try"}).name == "Second try"
        with pytest.raises(ValidationError):
            Rename.model_validate({"name": "Second try", "nmae": "typo"})
        ```
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _few_unknown_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and len(data) > len(cls.model_fields) + _MAX_UNKNOWN_FIELDS:
            unknown = len(data.keys() - cls.model_fields.keys())
            if unknown > _MAX_UNKNOWN_FIELDS:
                raise PydanticCustomError(
                    "extra_forbidden", "The object has {count} fields the model doesn't define.", {"count": unknown}
                )
        return data
