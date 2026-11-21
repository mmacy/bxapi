"""Dice rolls."""

from fastapi import APIRouter
from osrlib.core.dice import RollResult, roll
from osrlib.core.rng import RngStreams, StreamName
from pydantic import Field

from bxapi.errors import VALIDATION_ERROR, problem_responses
from bxapi.models import ApiModel, RequestModel
from bxapi.seeds import Seed, seed_or_draw

__all__ = ["DiceRollRequest", "DiceRollResponse", "router"]

router = APIRouter(prefix="/dice", tags=["dice"])


class DiceRollRequest(RequestModel):
    """The dice expression to roll, how many times, and an optional seed."""

    expression: str = Field(min_length=1, max_length=64, examples=["2d6+1x10"])
    """A dice expression like `3d6`, `d%`, `1d6+1`, or `2d6+1x10`.

    The form is the number of dice, `d`, and the number of sides, then an optional `+` or `-` modifier, then an
    optional multiplier after `x`, `×`, or `*`. The number of dice defaults to 1, and `d%` means `d100`. The sides are
    2, 3, 4, 6, 8, 10, 12, 20, or 100. The modifier applies before the multiplier, so `2d6+1x10` is (2d6 + 1) × 10.
    Case and spaces around the expression don't matter, and spaces inside it aren't allowed. The number of dice is 1
    to 999, the modifier is at most 999999 in size, and the multiplier is 1 to 999999. Numbers have no leading zeros.
    """
    times: int = Field(default=1, ge=1, le=100)
    """How many times to roll the expression."""
    seed: Seed | None = None


class DiceRollResponse(ApiModel):
    """The results of a dice roll, and the seed to send to get the same results again."""

    expression: str
    """The expression you sent, unchanged."""
    seed: Seed
    rolls: list[RollResult]
    """One result for each time, in the order they were rolled.

    Each result has the value of each die in `rolls`, the `modifier` and `multiplier` from the expression, and the
    `total`.
    """


@router.post(
    "/roll",
    summary="Roll a dice expression",
    response_description="One result for each time the server rolled the expression, and the seed it used.",
    responses=problem_responses(
        {
            422: [
                VALIDATION_ERROR,
                "`content_invalid`: osrlib can't read the expression, or the expression has a die size osrlib "
                "doesn't use, like `3d7`. `detail` says which. Fix the expression.",
            ]
        }
    ),
)
def roll_dice(body: DiceRollRequest) -> DiceRollResponse:
    """Roll a dice expression one or more times and return every die and every total.

    osrlib's `roll` function does the rolling. Use `total` of each result as the roll's value. The response includes
    the seed the server used. Send the same body with that seed to get the same results again, or leave `seed` out to
    get a new random seed. With the same seed, `times` of 3 returns the first three results of `times` of 5.

    The roll doesn't belong to a game, and the server stores nothing. In a session, osrlib makes the rolls the rules
    call for when you send a command, so you don't roll them here.
    """
    seed = seed_or_draw(body.seed)
    stream = RngStreams(master_seed=seed).get(StreamName.ADJUDICATION)
    return DiceRollResponse(
        expression=body.expression,
        seed=seed,
        rolls=[roll(body.expression, stream) for _ in range(body.times)],
    )
