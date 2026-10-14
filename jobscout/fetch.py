"""HTTP fetching: politeness, retries, conditional requests and proxy escalation.

* One shared httpx.AsyncClient (connection pooling, keep-alive) for the direct
  path, and a second one that routes through a proxy. A domain only moves to
  the proxy once its measured block rate crosses a threshold, because proxies
  cost money per GB and add latency.
* Conditional GETs: remember ETag / Last-Modified per URL and send
  If-None-Match / If-Modified-Since. A 304 is nearly free, which matters when
  the same boards are re-crawled every day.
* robots.txt is respected for HTML pages on company domains. Public ATS JSON
  APIs are designed for programmatic access, so they skip that check.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .politeness import RETRY_STATUSES, DomainGate, looks_like_block_page

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 jobscout/0.1"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class BlockedError(Exception):
    pass


class DomainTrippedError(Exception):
    pass


@dataclass
class Response:
    url: str
    status: int
    text: str
    headers: dict
    from_cache: bool = False

    def json(self):
        return json.loads(self.text)


@dataclass
class Fetcher:
    gate: DomainGate
    proxy_url: str | None = None
    proxy_block_rate: float = 0.3      # escalate a domain to the proxy above this block rate
    proxy_min_requests: int = 4        # ...once we have enough samples to trust it
    timeout_s: float = 20.0
    respect_robots: bool = True
    cache: dict = field(default_factory=dict)   # url -> {"etag", "last_modified", "body"}
    _robots: dict = field(default_factory=dict)
    _proxied_domains: set = field(default_factory=set)

    def __post_init__(self):
        self._direct = httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s,
                                         follow_redirects=True)
        self._proxied = (httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=self.timeout_s,
                                           follow_redirects=True, proxy=self.proxy_url)
                         if self.proxy_url else None)

    async def aclose(self):
        await self._direct.aclose()
        if self._proxied:
            await self._proxied.aclose()

    def _client_for(self, domain: str) -> httpx.AsyncClient:
        if self._proxied is None:
            return self._direct
        s = self.gate.stats[domain]
        if domain in self._proxied_domains or (
                s.requests >= self.proxy_min_requests and s.block_rate >= self.proxy_block_rate):
            self._proxied_domains.add(domain)
            return self._proxied
        return self._direct

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlparse(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                r = await self._direct.get(base + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._robots[base] = rp
        return self._robots[base].can_fetch(USER_AGENT, url)

    async def request(self, method: str, url: str, *, json_body=None, check_robots=False,
                      conditional=True) -> Response:
        domain = urlparse(url).netloc
        if self.gate.is_tripped(domain):
            raise DomainTrippedError(domain)
        if check_robots and not await self.allowed(url):
            raise BlockedError(f"robots.txt disallows {url}")

        headers = {}
        cached = self.cache.get(url) if (conditional and method == "GET") else None
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]

        last_exc: Exception | None = None
        for attempt in range(self.gate.cfg.max_retries + 1):
            async with self.gate.semaphore(domain):
                await self.gate.wait_turn(domain)
                client = self._client_for(domain)
                t0 = time.perf_counter()
                try:
                    r = await client.request(method, url, headers=headers, json=json_body)
                except httpx.HTTPError as exc:
                    self.gate.record(domain, None, (time.perf_counter() - t0) * 1000)
                    last_exc = exc
                    await asyncio.sleep(self.gate.backoff_delay(attempt))
                    continue
                latency = (time.perf_counter() - t0) * 1000
                body = r.text if r.status_code != 304 else ""
                blocked = looks_like_block_page(r.status_code, body)
                self.gate.record(domain, r.status_code, latency, len(r.content), blocked=blocked)

            if r.status_code == 304 and cached:
                return Response(url, 200, cached["body"], dict(r.headers), from_cache=True)
            if blocked or r.status_code in RETRY_STATUSES:
                if self.gate.is_tripped(domain):
                    raise DomainTrippedError(domain)
                last_exc = BlockedError(f"{r.status_code} from {url}") if blocked else httpx.HTTPStatusError(
                    f"{r.status_code}", request=r.request, response=r)
                await asyncio.sleep(self.gate.backoff_delay(attempt, r.headers.get("Retry-After")))
                continue
            if r.status_code >= 400:
                r.raise_for_status()
            if method == "GET" and (r.headers.get("ETag") or r.headers.get("Last-Modified")):
                self.cache[url] = {"etag": r.headers.get("ETag"),
                                   "last_modified": r.headers.get("Last-Modified"),
                                   "body": r.text}
            return Response(url, r.status_code, r.text, dict(r.headers))
        raise last_exc or BlockedError(url)

    async def get(self, url: str, **kw) -> Response:
        return await self.request("GET", url, **kw)

    async def post_json(self, url: str, body: dict, **kw) -> Response:
        return await self.request("POST", url, json_body=body, conditional=False, **kw)
