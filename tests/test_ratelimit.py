from app.ratelimit import RateLimiter


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:  # a fixed UTC moment
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_per_client_window_slides():
    clock = Clock()
    limiter = RateLimiter(per_client=2, window_s=600, daily_cap=0, clock=clock)
    assert limiter.check("a") is None and limiter.check("a") is None
    assert "wait about 10 min" in limiter.check("a")
    assert limiter.check("b") is None  # other visitors are unaffected
    clock.t += 601
    assert limiter.check("a") is None


def test_refused_requests_do_not_consume_quota():
    clock = Clock()
    limiter = RateLimiter(per_client=1, window_s=60, daily_cap=0, clock=clock)
    limiter.check("a")
    for _ in range(5):
        assert limiter.check("a")
    clock.t += 61
    assert limiter.check("a") is None


def test_daily_cap_is_global_and_resets_at_utc_midnight():
    clock = Clock()
    limiter = RateLimiter(per_client=0, window_s=60, daily_cap=2, clock=clock)
    assert limiter.check("a") is None and limiter.check("b") is None
    assert "daily limit of 2" in limiter.check("c")
    clock.t += 86_400
    assert limiter.check("c") is None


def test_zero_disables_limits():
    limiter = RateLimiter(per_client=0, window_s=60, daily_cap=0)
    assert all(limiter.check("a") is None for _ in range(100))
