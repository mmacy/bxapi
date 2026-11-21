"""The base classes for the models the API defines."""

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_core import PydanticCustomError

__all__ = ["ApiModel", "RequestModel"]

_MAX_UNKNOWN_FIELDS = 20


class ApiModel(BaseModel):
    """The base class of every response model that bxapi defines.

    Document each field with a docstring on the line after it. pydantic copies that docstring
    into the field's description, and FastAPI copies the description into the OpenAPI document,
    which is the reference a client developer reads.

    A response that is an osrlib model is returned as osrlib defines it, and doesn't extend this
    class.

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
    check, a misspelled field name would be dropped, and the server would act on the default.
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
