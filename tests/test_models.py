import pytest
from pydantic import ValidationError

from bxapi.models import RequestModel


class _Rename(RequestModel):
    name: str


def test_request_models_refuse_unknown_fields() -> None:
    assert _Rename.model_validate({"name": "Second try"}).name == "Second try"
    with pytest.raises(ValidationError, match="nmae"):
        _Rename.model_validate({"name": "Second try", "nmae": "typo"})
