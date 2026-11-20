"""The base class for the request bodies the API defines."""

from pydantic import BaseModel, ConfigDict

__all__ = ["RequestModel"]


class RequestModel(BaseModel):
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
