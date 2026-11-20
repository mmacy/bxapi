import pytest

from bxapi.auth import Principal
from bxapi.errors import ApiError
from bxapi.seeds import MAX_SEED, draw_seed, referee_seed, seed_or_draw
from bxapi.settings import Role

PLAYER = Principal(client="game", role=Role.PLAYER)
REFEREE = Principal(client="game", role=Role.REFEREE)


def test_drawn_seeds_are_in_range_and_differ() -> None:
    seeds = {draw_seed() for _ in range(50)}
    assert len(seeds) == 50
    assert all(0 <= seed <= MAX_SEED for seed in seeds)


def test_seed_or_draw_prefers_the_callers_seed() -> None:
    assert seed_or_draw(0) == 0
    assert seed_or_draw(7) == 7
    assert 0 <= seed_or_draw(None) <= MAX_SEED


def test_only_a_referee_chooses_a_stored_seed() -> None:
    assert referee_seed(0, REFEREE) == 0
    assert 0 <= referee_seed(None, PLAYER) <= MAX_SEED
    with pytest.raises(ApiError) as refused:
        referee_seed(42, PLAYER)
    assert (refused.value.status, refused.value.code) == (403, "referee_required")


def test_a_javascript_client_reads_every_seed_exactly() -> None:
    assert MAX_SEED == 2**53 - 1
    assert float(MAX_SEED) == MAX_SEED
