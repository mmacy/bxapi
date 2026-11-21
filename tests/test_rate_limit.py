import logging
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from bxapi.errors import PROBLEM_MEDIA_TYPE
from bxapi.rate_limit import NO_KEY, RateLimiter, rate_limited
from bxapi.settings import Settings

from .conftest import OTHER_KEY, PLAYER_KEY, REFEREE_KEY, bearer, problem_without_id


class Clock:
    """A clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# The limiter


def test_a_budget_starts_full_and_refills_at_a_steady_rate() -> None:
    clock = Clock()
    limiter = RateLimiter(120, clock=clock)
    assert [limiter.take("game") for _ in range(120)] == [0] * 120
    assert limiter.take("game") == 1
    clock.now += 0.25
    assert limiter.take("game") == 1
    clock.now += 0.25
    assert limiter.take("game") == 0
    assert limiter.take("game") == 1
    clock.now += 10
    assert [limiter.take("game") for _ in range(21)] == [0] * 20 + [1]


def test_a_budget_never_holds_more_than_a_minute_of_requests() -> None:
    clock = Clock()
    limiter = RateLimiter(3, clock=clock)
    assert limiter.take("game") == 0
    clock.now += 3600
    assert [limiter.take("game") for _ in range(4)] == [0, 0, 0, 20]


def test_the_wait_is_the_whole_seconds_until_one_request() -> None:
    clock = Clock()
    limiter = RateLimiter(2, clock=clock)
    assert [limiter.take("game") for _ in range(3)] == [0, 0, 30]
    clock.now += 12.5
    assert limiter.take("game") == 18
    clock.now += 17.5
    assert limiter.take("game") == 0


def test_each_name_has_its_own_budget_and_a_refused_request_costs_nothing() -> None:
    clock = Clock()
    limiter = RateLimiter(1, clock=clock)
    assert limiter.take("game") == 0
    assert [limiter.take("game") for _ in range(50)] == [60] * 50
    assert limiter.take("other") == 0
    assert limiter.take(NO_KEY) == 0
    clock.now += 60
    assert limiter.take("game") == 0


def test_a_limit_of_zero_is_no_limit() -> None:
    limiter = RateLimiter(0, clock=Clock())
    assert [limiter.take("game") for _ in range(10_000)] == [0] * 10_000


# The server


@pytest.fixture
def limited(keyed_settings: Settings) -> Settings:
    """Settings with three API keys and a limit of three requests a minute."""
    return keyed_settings.model_copy(update={"rate_limit_per_minute": 3})


def test_a_client_over_the_limit_gets_429_with_the_seconds_to_wait(
    limited: Settings, make_client: Callable[..., TestClient]
) -> None:
    client = make_client(limited)
    for _ in range(3):
        assert client.get("/v1/meta", headers=bearer(PLAYER_KEY)).status_code == 200
    refused = client.get("/v1/meta", headers=bearer(PLAYER_KEY))
    assert refused.status_code == 429
    assert refused.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert refused.headers["retry-after"] == "20"
    assert problem_without_id(refused) == rate_limited(3, 20).problem().model_dump(mode="json")


def test_the_keys_of_one_client_share_a_budget_and_another_client_has_its_own(
    limited: Settings, make_client: Callable[..., TestClient]
) -> None:
    client = make_client(limited)
    statuses = [client.get("/v1/meta", headers=bearer(key)).status_code for key in (PLAYER_KEY, REFEREE_KEY) * 2]
    assert statuses == [200, 200, 200, 429]
    assert client.get("/v1/meta", headers=bearer(OTHER_KEY)).status_code == 200


def test_requests_with_no_valid_key_share_a_budget_of_their_own(
    limited: Settings, make_client: Callable[..., TestClient]
) -> None:
    client = make_client(limited)
    wrong = bearer("no-such-key-0123456789")
    statuses = [client.get("/v1/meta", headers=headers).status_code for headers in ({}, wrong, {}, wrong, {})]
    assert statuses == [401, 401, 401, 429, 429]
    refused = client.get("/v1/meta")
    assert problem_without_id(refused) == rate_limited(3, 20, has_key=False).problem().model_dump(mode="json")
    assert "no valid API key" in refused.json()["detail"]
    assert client.get("/v1/meta", headers=bearer(PLAYER_KEY)).status_code == 200


def test_a_refused_request_runs_nothing_and_claims_no_idempotency_key(
    limited: Settings, make_client: Callable[..., TestClient]
) -> None:
    client = make_client(limited, PLAYER_KEY)
    for _ in range(3):
        assert client.get("/v1/characters").json()["total"] == 0
    fighter = {"name": "Hild", "class_id": "fighter", "alignment": "lawful"}
    refused = client.post("/v1/characters", json=fighter, headers={"Idempotency-Key": "create"})
    assert (refused.status_code, refused.json()["code"]) == (429, "rate_limited")
    assert client.post("/v1/dice/roll", content=b"not json").status_code == 429
    other = make_client(limited, OTHER_KEY)
    assert other.get("/v1/characters").json()["total"] == 0


def test_the_paths_that_need_no_key_and_a_preflight_cost_nothing(
    limited: Settings, make_client: Callable[..., TestClient]
) -> None:
    allowed = "https://game.example"
    client = make_client(limited.model_copy(update={"cors_origins": (allowed,)}), PLAYER_KEY)
    preflight = {"Origin": allowed, "Access-Control-Request-Method": "POST"}
    for _ in range(10):
        assert client.get("/health").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert client.options("/v1/dice/roll", headers=preflight).status_code == 200
    assert [client.get("/v1/meta").status_code for _ in range(4)] == [200, 200, 200, 429]


def test_a_server_with_no_keys_limits_its_one_client_and_not_another_host(
    settings: Settings, make_client: Callable[..., TestClient]
) -> None:
    client = make_client(settings.model_copy(update={"rate_limit_per_minute": 2}))
    for _ in range(5):
        assert client.get("/v1/meta", headers={"Host": "game.example"}).status_code == 421
    assert [client.get("/v1/meta").status_code for _ in range(3)] == [200, 200, 429]


def test_a_limit_of_zero_turns_the_limit_off(limited: Settings, make_client: Callable[..., TestClient]) -> None:
    client = make_client(limited.model_copy(update={"rate_limit_per_minute": 0}), PLAYER_KEY)
    assert {client.get("/v1/meta").status_code for _ in range(50)} == {200}
    assert {client.get("/v1/meta", headers={"Authorization": ""}).status_code for _ in range(50)} == {401}


def test_the_default_limit_is_600_and_a_negative_limit_is_refused() -> None:
    assert Settings.model_fields["rate_limit_per_minute"].default == 600
    with pytest.raises(ValueError):
        Settings(database=":memory:", allow_anonymous=True, rate_limit_per_minute=-1)


def test_the_log_line_of_a_refused_request_names_the_client(
    limited: Settings, make_client: Callable[..., TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(limited)
    with caplog.at_level(logging.INFO, logger="bxapi.access"):
        for _ in range(4):
            client.get("/v1/meta", headers=bearer(PLAYER_KEY))
        for _ in range(4):
            client.get("/v1/meta")
    lines = [record.getMessage() for record in caplog.records if record.name == "bxapi.access"]
    assert "status=429" in lines[3] and lines[3].endswith("client=game")
    assert "status=429" in lines[7] and lines[7].endswith("client=-")
