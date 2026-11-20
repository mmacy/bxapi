"""The page format every list endpoint returns, and the query parameters that select a page."""

from collections.abc import Sequence
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel, Field

from bxapi.models import ApiModel

__all__ = ["Page", "PageParams", "PageQuery", "paginate"]

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


class PageParams(BaseModel):
    """The `limit` and `offset` query parameters of a list endpoint."""

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    """The largest number of items to return."""
    offset: int = Field(default=0, ge=0)
    """How many items to skip before the first one returned."""


def _page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="The largest number of items to return.")] = 50,
    offset: Annotated[int, Query(ge=0, description="How many items to skip before the first one returned.")] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


PageQuery = Annotated[PageParams, Depends(_page_params)]
"""Annotate an endpoint parameter with this type to read `limit` and `offset` from the query."""


class Page[Item](ApiModel):
    """One page of a list, with the size of the whole list."""

    items: list[Item]
    """The items on this page, in the list's order."""
    total: int
    """How many items the whole list has."""
    limit: int
    """The `limit` the request asked for."""
    offset: int
    """The `offset` the request asked for."""


def paginate[Item](items: Sequence[Item], params: PageParams) -> Page[Item]:
    """Return one page of a list that's already in memory.

    Use it for a list the server builds from the rules data. An endpoint that reads from the
    database asks the database for one page instead.

    Args:
        items: The whole list.
        params: The page to return.

    Returns:
        The page, with `total` set to the length of `items`.

    Examples:
        ```python
        from bxapi.pagination import PageParams, paginate

        page = paginate(["a", "b", "c"], PageParams(limit=2, offset=1))
        assert page.items == ["b", "c"]
        assert page.total == 3
        ```
    """
    return Page(
        items=list(items[params.offset : params.offset + params.limit]),
        total=len(items),
        limit=params.limit,
        offset=params.offset,
    )
