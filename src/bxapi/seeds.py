"""Seeds for the random numbers osrlib draws.

osrlib draws every random number from a stream it derives from one integer, the *seed*. The
same seed and the same requests give the same results, so whoever chooses a seed can work out
every roll in advance.

The API treats seeds in two ways:

- A stateless endpoint, like a dice roll, accepts a seed from any caller and returns the seed
  it used, so the caller can repeat the roll. Call
  [`seed_or_draw`][bxapi.seeds.seed_or_draw].
- An endpoint that creates something a game plays with, like a stored character or a session,
  accepts a seed only from a referee and never returns it. Call
  [`referee_seed`][bxapi.seeds.referee_seed].
"""

import secrets
from typing import Annotated

from pydantic import Field

from bxapi.auth import Principal
from bxapi.errors import forbidden

__all__ = ["MAX_SEED", "Seed", "draw_seed", "referee_seed", "seed_or_draw"]

MAX_SEED = 2**63 - 1
"""The largest seed the API accepts."""

Seed = Annotated[
    int,
    Field(ge=0, le=MAX_SEED, description="The seed for the random numbers, an integer from 0 to 2^63 - 1."),
]
"""The type of a `seed` field in a request or response model."""


def draw_seed() -> int:
    """Return a new seed from the operating system's random source.

    Returns:
        An integer from 0 to [`MAX_SEED`][bxapi.seeds.MAX_SEED].
    """
    return secrets.randbits(63)


def seed_or_draw(seed: int | None) -> int:
    """Return the seed a stateless endpoint uses: the caller's, or a new one.

    Args:
        seed: The seed from the request, or `None` if the request had none.

    Returns:
        `seed` when it isn't `None`, and a new seed otherwise. Include it in the response.

    Examples:
        ```python
        from bxapi.seeds import seed_or_draw

        assert seed_or_draw(7) == 7
        assert seed_or_draw(None) >= 0
        ```
    """
    return seed if seed is not None else draw_seed()


def referee_seed(seed: int | None, principal: Principal) -> int:
    """Return the seed for something a game plays with, and refuse a seed from a player.

    Args:
        seed: The seed from the request, or `None` if the request had none.
        principal: The caller.

    Returns:
        `seed` when a referee passed one, and a new seed otherwise. Never include it in a
        response.

    Raises:
        ApiError: With status 403 and the code `referee_required`, if a player passed a seed.

    Examples:
        ```python
        from bxapi.auth import Principal
        from bxapi.seeds import referee_seed
        from bxapi.settings import Role

        referee = Principal(client="game", role=Role.REFEREE)
        assert referee_seed(42, referee) == 42
        ```
    """
    if seed is None:
        return draw_seed()
    if not principal.is_referee:
        raise forbidden("referee_required", "Only an API key with the referee role can choose a seed.")
    return seed
