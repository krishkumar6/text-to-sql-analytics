"""In-memory request limits for a public demo: a per-client sliding window plus a global daily cap.

Protects the LLM budget (e.g. Groq's free-tier daily token quota) from one visitor or a crawler.
State lives in process memory, which is right for a single-instance demo; with several replicas,
move it to Redis or put limits in the reverse proxy.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Callable


class RateLimiter:
    def __init__(self, per_client: int, window_s: int, daily_cap: int,
                 clock: Callable[[], float] = time.time) -> None:
        self.per_client = per_client  # 0 disables the per-client limit
        self.window_s = window_s
        self.daily_cap = daily_cap  # 0 disables the daily cap
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._day = ""
        self._day_count = 0

    def check(self, client: str) -> str | None:
        """Record one request. Returns a message for the user when it must be refused, else None."""
        now = self.clock()
        today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self._lock:
            if today != self._day:
                self._day, self._day_count = today, 0
            if self.daily_cap and self._day_count >= self.daily_cap:
                return (f"This demo has reached its daily limit of {self.daily_cap} questions (it runs on a "
                        "free LLM tier). Please try again tomorrow, or run it locally from the GitHub repo.")
            if self.per_client:
                hits = self._hits[client]
                while hits and hits[0] <= now - self.window_s:
                    hits.popleft()
                if len(hits) >= self.per_client:
                    wait_min = max(1, round((hits[0] + self.window_s - now) / 60))
                    return (f"You've asked {self.per_client} questions in the last {self.window_s // 60} minutes. "
                            f"Please wait about {wait_min} min.")
                hits.append(now)
            self._day_count += 1
            return None
