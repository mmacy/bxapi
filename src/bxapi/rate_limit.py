"""How many requests a client may send in a minute.

Every request costs the server time on a worker thread, and a client that sends them as fast as
it can leaves none for the other clients. [`RateLimiter`][bxapi.rate_limit.RateLimiter] gives
each client a budget of requests that refills at a steady rate, and
[`AuthMiddleware`][bxapi.auth.AuthMiddleware] takes one request from the budget for each
request under `/v1`. A client with nothing left gets 429 `rate_limited`, with a `Retry-After`
header.

The budgets are in the memory of one server process. Two processes that share a database file
each give a client the whole budget.
"""

import math
import threading
import time
from collections.abc import Callable

from bxapi.errors import ApiError

__all__ = ["NO_KEY", "RateLimiter", "rate_limited"]

NO_KEY = ""
"""The budget name for every request that has no valid API key. No client has an empty name."""


def rate_limited(per_minute: int, retry_after: int, *, has_key: bool = True) -> ApiError:
    """Build the 429 for a request over the rate limit.

    Args:
        per_minute: The server's limit, in requests a minute.
        retry_after: The seconds until the budget has one request again.
        has_key: Whether the request had a valid API key. Requests without one share a budget.

    Returns:
        An [`ApiError`][bxapi.errors.ApiError] with the code `rate_limited` and a `Retry-After`
        header.
    """
    who = "Your client sent" if has_key else "The server got"
    kind = "requests" if has_key else "requests with no valid API key"
    seconds = "1 second" if retry_after == 1 else f"{retry_after} seconds"
    return ApiError(
        429,
        "rate_limited",
        f"{who} more than {per_minute} {kind} in a minute. Wait {seconds} and send the request again.",
        headers={"Retry-After": str(retry_after)},
    )


class RateLimiter:
    """A budget of requests for each client, which refills at a steady rate.

    A budget starts full, at `per_minute` requests. Each request takes one, and the budget
    regains `per_minute` requests a minute, a fraction at a time, up to full. So a client can
    send a burst of `per_minute` requests at once, and `per_minute` requests a minute after
    that. The scheme is called a token bucket.

    Args:
        per_minute: The size of a budget, and how many requests it regains in a minute. 0 means no
            limit.
        clock: The function that returns the current time in seconds. It must never go backward.

    Examples:
        ```python
        from bxapi.rate_limit import RateLimiter

        now = 0.0
        limiter = RateLimiter(2, clock=lambda: now)
        assert limiter.take("my-game") == 0
        assert limiter.take("my-game") == 0
        assert limiter.take("my-game") == 30
        assert limiter.take("another-game") == 0
        now = 30.0
        assert limiter.take("my-game") == 0
        ```
    """

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._lock = threading.Lock()
        # Each budget is the requests left and the time of that count. The server has one budget for each
        # client in its settings and one for requests with no key, so the dict never grows past that.
        self._budgets: dict[str, tuple[float, float]] = {}

    def take(self, name: str) -> int:
        """Take one request from a budget, if it has one.

        Args:
            name: The budget's name: a client's name, or [`NO_KEY`][bxapi.rate_limit.NO_KEY].

        Returns:
            0 if the budget had a request, and the caller may go on. Otherwise the whole seconds
            until the budget has one, at least 1, and the budget is unchanged.
        """
        if self.per_minute == 0:
            return 0
        per_second = self.per_minute / 60
        with self._lock:
            now = self._clock()
            left, counted_at = self._budgets.get(name, (float(self.per_minute), now))
            left = min(float(self.per_minute), left + (now - counted_at) * per_second)
            if left >= 1:
                self._budgets[name] = (left - 1, now)
                return 0
            self._budgets[name] = (left, now)
            return max(1, math.ceil((1 - left) / per_second))
