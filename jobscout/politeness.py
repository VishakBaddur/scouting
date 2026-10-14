"""Per-domain politeness, backoff and block tracking.

Getting blocked is mostly a consequence of hammering one host. Every request
goes through a DomainGate, which enforces:

* a per-domain concurrency cap (asyncio.Semaphore)
* a minimum interval between requests to the same domain, with jitter
* exponential backoff with full jitter on 429 / 5xx / blocks, honouring Retry-After
* a circuit breaker: after N consecutive blocks we stop hitting that domain
  for the rest of the run instead of burning requests (and proxy budget)
* block-rate stats that drive proxy escalation (see fetch.py)
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

BLOCK_STATUSES = {401, 403, 429, 503}
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class DomainStats:
    requests: int = 0
    ok: int = 0
    blocked: int = 0
    errors: int = 0
    not_modified: int = 0
    bytes: int = 0
    latency_ms_total: float = 0.0
    consecutive_blocks: int = 0
    tripped: bool = False

    @property
    def block_rate(self) -> float:
        return self.blocked / self.requests if self.requests else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.latency_ms_total / self.requests if self.requests else 0.0


@dataclass
class PolitenessConfig:
    min_interval_s: float = 1.0
    jitter_s: float = 0.5
    max_concurrency_per_domain: int = 2
    max_retries: int = 4
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    trip_after_consecutive_blocks: int = 5
    # public JSON APIs meant for programmatic use can be hit faster
    fast_domains: tuple[str, ...] = (
        "boards-api.greenhouse.io", "api.lever.co", "api.ashbyhq.com", "api.smartrecruiters.com",
    )
    fast_min_interval_s: float = 0.2
    # JSON endpoints behind JS career sites: lighter than HTML pages, so a moderate pace
    medium_domains: tuple[str, ...] = ("myworkdayjobs.com",)
    medium_min_interval_s: float = 0.4


class DomainGate:
    def __init__(self, cfg: PolitenessConfig | None = None, clock=time.monotonic, sleep=asyncio.sleep):
        self.cfg = cfg or PolitenessConfig()
        self._clock = clock
        self._sleep = sleep
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_ok: dict[str, float] = defaultdict(float)
        self.stats: dict[str, DomainStats] = defaultdict(DomainStats)

    def _interval(self, domain: str) -> float:
        if any(domain.endswith(d) for d in self.cfg.fast_domains):
            return min(self.cfg.fast_min_interval_s, self.cfg.min_interval_s)
        if any(domain.endswith(d) for d in self.cfg.medium_domains):
            return min(self.cfg.medium_min_interval_s, self.cfg.min_interval_s)
        return self.cfg.min_interval_s

    def semaphore(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._sems:
            self._sems[domain] = asyncio.Semaphore(self.cfg.max_concurrency_per_domain)
        return self._sems[domain]

    async def wait_turn(self, domain: str) -> None:
        """Block until this domain may be hit again (spacing + jitter)."""
        async with self._locks[domain]:
            wait = self._next_ok[domain] - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._next_ok[domain] = self._clock() + self._interval(domain) + random.uniform(0, self.cfg.jitter_s)

    def backoff_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            delay = _parse_retry_after(retry_after)
            if delay is not None:
                return min(delay, self.cfg.backoff_cap_s)
        # exponential backoff with "full jitter"
        ceiling = min(self.cfg.backoff_cap_s, self.cfg.backoff_base_s * (2 ** attempt))
        return random.uniform(0, ceiling)

    def record(self, domain: str, status: int | None, latency_ms: float, nbytes: int = 0, blocked: bool = False) -> None:
        s = self.stats[domain]
        s.requests += 1
        s.latency_ms_total += latency_ms
        s.bytes += nbytes
        if blocked or (status in BLOCK_STATUSES):
            s.blocked += 1
            s.consecutive_blocks += 1
            if s.consecutive_blocks >= self.cfg.trip_after_consecutive_blocks:
                s.tripped = True
        elif status is None or status >= 400:
            s.errors += 1
        else:
            s.consecutive_blocks = 0
            if status == 304:
                s.not_modified += 1
            else:
                s.ok += 1

    def is_tripped(self, domain: str) -> bool:
        return self.stats[domain].tripped


def _parse_retry_after(value: str) -> float | None:
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def looks_like_block_page(status: int, body: str) -> bool:
    """Some anti-bot layers return 200 with a challenge page instead of 403."""
    if status in BLOCK_STATUSES:
        return True
    if status == 200 and len(body) < 20000:
        lower = body.lower()
        markers = ("cf-chl", "challenge-platform", "attention required", "access denied",
                   "please verify you are a human", "captcha", "_incapsula_", "px-captcha")
        return any(m in lower for m in markers)
    return False
