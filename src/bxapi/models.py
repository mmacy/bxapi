"""The base classes for the models the API defines."""

from pydantic import BaseModel, ConfigDict

__all__ = ["ApiModel", "RequestModel"]


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
