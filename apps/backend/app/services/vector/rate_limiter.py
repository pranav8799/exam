"""Pacing of paid API calls made by search-index runs (Jina reranker).

Jina limits a key's concurrent requests (2 on the current key: HTTP 429 "RATE_CONCURRENCY_LIMIT_EXCEEDED")
and its tokens per minute; a run that sends too much gets HTTP 429 and, after retries, stops.
Instead the run paces itself:
- `slot()` keeps at most `max_concurrent` requests in flight (default 1, leaving the key's other
  slot for students' live page views);
- `acquire(estimate)` waits until the last 60 s of usage plus this request fits in the budget;
- `record(estimate, actual)` replaces the estimate with the token count Jina reports;
- `rate_limited()` (on a 429) lowers the budget by a quarter and makes every caller wait until the
  window has cleared, so the run settles just under the key's real limit, whatever its tier.
Live page views do not use it.
"""
import asyncio
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Callable, Deque, Tuple

WINDOW_SECONDS = 60.0
# Share of the key's tokens-per-minute a run may use; the rest stays free for students' page views
RUN_SHARE_OF_KEY_LIMIT = 0.8
_LIMIT_RE = re.compile(r"([\d,]+)\s*/\s*([\d,]+)\s*tokens per minute", re.IGNORECASE)


def key_limit_from_message(text: str):
    """The key's tokens-per-minute limit from Jina's 429 text ("... 101,801/100,000 tokens per minute ..."), or None."""
    m = _LIMIT_RE.search(text or "")
    return int(m.group(2).replace(",", "")) if m else None


class TokenRateLimiter:
    def __init__(self, tokens_per_minute: int, min_tokens_per_minute: int = 20_000, max_concurrent: int = 1,
                 clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep):
        self.budget = max(1, int(tokens_per_minute))
        self.min_budget = max(1, min(int(min_tokens_per_minute), self.budget))
        self.rate_limit_hits = 0
        self._clock, self._sleep = clock, sleep
        self._events: Deque[Tuple[float, int]] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()
        self.max_concurrent = max(1, int(max_concurrent))
        self._slots = asyncio.Semaphore(self.max_concurrent)

    @asynccontextmanager
    async def slot(self):
        """At most `max_concurrent` requests in flight."""
        async with self._slots:
            yield

    def _used(self, now: float) -> int:
        while self._events and now - self._events[0][0] >= WINDOW_SECONDS:
            self._events.popleft()
        return sum(t for _, t in self._events)

    async def acquire(self, tokens: int) -> None:
        tokens = max(1, int(tokens))
        while True:
            async with self._lock:
                now = self._clock()
                if now >= self._blocked_until:
                    used = self._used(now)
                    # A request bigger than the whole budget still goes through when the window is empty
                    if used + tokens <= self.budget or not self._events:
                        self._events.append((now, tokens))
                        return
                    wait = WINDOW_SECONDS - (now - self._events[0][0]) + 0.05
                else:
                    wait = self._blocked_until - now
            await self._sleep(max(0.05, wait))

    def record(self, estimated: int, actual: int) -> None:
        """Swap the newest matching estimate for the real token count."""
        if actual <= 0:
            return
        for i in range(len(self._events) - 1, -1, -1):
            ts, tok = self._events[i]
            if tok == max(1, int(estimated)):
                self._events[i] = (ts, int(actual))
                return

    def rate_limited(self, message: str = "") -> float:
        """A 429 arrived: lower the budget and block everyone until the window clears. Returns the wait.

        If the message states the key's limit, the budget becomes a fixed share of it; otherwise it
        drops by a quarter.
        """
        self.rate_limit_hits += 1
        key_limit = key_limit_from_message(message)
        if key_limit:
            self.budget = max(self.min_budget, min(self.budget, int(key_limit * RUN_SHARE_OF_KEY_LIMIT)))
        else:
            self.budget = max(self.min_budget, int(self.budget * 0.75))
        now = self._clock()
        self._blocked_until = max(self._blocked_until, now + WINDOW_SECONDS)
        self._events.clear()
        return self._blocked_until - now
