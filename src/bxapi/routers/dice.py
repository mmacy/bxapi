"""Dice rolls."""

from fastapi import APIRouter
from osrlib.core.dice import RollResult, roll
from osrlib.core.rng import RngStreams, StreamName
from pydantic import Field

from bxapi.errors import VALIDATION_ERROR, problem_responses
from bxapi.idempotency import IdempotentRoute
from bxapi.models import ApiModel, RequestModel
from bxapi.seeds import Seed, seed_or_draw

__all__ = ["DiceRollRequest", "DiceRollResponse", "router"]

router = APIRouter(prefix="/dice", tags=["dice"], route_class=IdempotentRoute)


class DiceRollRequest(RequestModel):
    """Dice expression, roll count, and optional seed for a stateless roll."""

    expression: str = Field(min_length=1, max_length=64, examples=["2d6+1x10"])
    """Dice expression such as `3d6`, `d%`, `1d6+1`, or `2d6+1x10`.

    Format: `[count]d<sides>[+|-<modifier>][x|×|*<multiplier>]`. `count` is 1 to 999 (defaults to
    1). `sides` is `2`, `3`, `4`, `6`, `8`, `10`, `12`, `20`, `100`, or `%` (`d100`). `modifier` is
    at most 999,999 in magnitude and applies before `multiplier` (1 to 999,999), so `2d6+1x10`
    computes `(2d6 + 1) × 10`. osrlib ignores leading and trailing whitespace and letter case, while
    rejecting internal spaces and leading zeros with 422 `content_invalid`.
    """
    times: int = Field(default=1, ge=1, le=100)
    """Number of independent rolls of `expression` to make, from 1 to 100. Defaults to 1."""
    seed: Seed | None = None
    """Optional seed (0 to 2^53 - 1) for deterministic results. Pass the `seed` from an earlier
    response with the same `expression` and `times` to repeat the roll, or omit it to let the
    server draw a random seed."""


class DiceRollResponse(ApiModel):
    """Rolled dice results and the seed that produced them."""

    expression: str
    """Dice expression from the request, unchanged."""
    seed: Seed
    rolls: list[RollResult]
    """One `RollResult` per roll (`times` entries in roll order), each containing individual die
    values in `rolls`, `modifier`, `multiplier`, and `total`."""


@router.post(
    "/roll",
    summary="Roll a dice expression",
    response_description="Roll results in roll order and the seed used.",
    responses=problem_responses(
        {
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: osrlib can't parse `expression`, or `expression` uses an unsupported die size "
                "such as `3d7`. Check `detail` and send a supported B/X dice expression.",
            ]
        }
    ),
)
def roll_dice(body: DiceRollRequest) -> DiceRollResponse:
    """Roll a B/X dice expression one or more times with osrlib's `roll` function and return every die and total.

    Read each entry's `total` in `rolls` for the final value, and `rolls[i].rolls` for the individual dice. Both
    `player` and `referee` keys can pass `seed`. Sending the same request body with the returned `seed` reproduces
    the exact sequence on the same osrlib version, and `times: 3` returns the first three results of `times: 5` for
    the same `seed`.

    The server stores nothing (unless you pass `Idempotency-Key`). During a game session, osrlib rolls all required
    dice automatically when you call `POST /v1/sessions/{session_id}/commands`. To log a custom referee roll inside
    a session, send the `roll_dice` command there instead.
    """
    seed = seed_or_draw(body.seed)
    stream = RngStreams(master_seed=seed).get(StreamName.ADJUDICATION)
    return DiceRollResponse(
        expression=body.expression,
        seed=seed,
        rolls=[roll(body.expression, stream) for _ in range(body.times)],
    )
