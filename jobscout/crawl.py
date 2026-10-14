"""Crawl orchestrator: many companies concurrently, one politeness gate shared by all."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass

import yaml

from .adapters.ats import ADAPTERS
from .adapters.browser import scrape_career_page
from .detect import Board, detect_from_url
from .fetch import Fetcher
from .politeness import DomainGate, PolitenessConfig
from .store import Store


@dataclass
class CompanyResult:
    company: str
    method: str = ""
    jobs: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    closed: int = 0
    error: str = ""
    seconds: float = 0.0


def load_companies(path: str) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("companies", data) if isinstance(data, dict) else data


async def crawl_company(fetcher: Fetcher, store: Store, entry: dict, use_browser: bool,
                        proxy: str | None) -> CompanyResult:
    name = entry["name"]
    res = CompanyResult(name)
    t0 = time.perf_counter()
    try:
        board = None
        if entry.get("ats") and entry.get("token"):
            board = Board(entry["ats"], entry["token"], entry.get("url", ""))
        elif entry.get("url"):
            board = detect_from_url(entry["url"])

        jobs = []
        if board:
            res.method = f"api:{board.source}"
        else:
            board, jobs, how = await scrape_career_page(fetcher, entry["url"], name, use_browser, proxy)
            res.method = how
            if board:
                res.method += f"->api:{board.source}"
        if board:
            jobs = await ADAPTERS[board.source](fetcher, board, name)

        # Never mark everything closed because a fetch silently returned nothing.
        if not jobs and entry.get("expect_jobs", True):
            res.error = "no jobs found (not updating store)"
        else:
            up = store.upsert_company_jobs(name, jobs)
            res.jobs = len(jobs)
            res.new, res.updated, res.unchanged, res.closed = up.new, up.updated, up.unchanged, up.closed
    except Exception as exc:  # one bad company must not kill the run
        res.error = f"{type(exc).__name__}: {exc}".splitlines()[0][:120]
    res.seconds = round(time.perf_counter() - t0, 2)
    return res


async def crawl(companies: list[dict], db_path: str = "jobs.db", concurrency: int = 8,
                use_browser: bool = True, proxy: str | None = None,
                cfg: PolitenessConfig | None = None) -> dict:
    store = Store(db_path)
    gate = DomainGate(cfg)
    fetcher = Fetcher(gate, proxy_url=proxy, cache=store.load_http_cache())
    run_id = store.start_run()
    sem = asyncio.Semaphore(concurrency)
    t0 = time.perf_counter()

    async def one(entry):
        async with sem:
            return await crawl_company(fetcher, store, entry, use_browser, proxy)

    try:
        results = await asyncio.gather(*(one(e) for e in companies))
    finally:
        store.save_http_cache(fetcher.cache)
        await fetcher.aclose()

    domains = {d: {"requests": s.requests, "ok": s.ok, "not_modified": s.not_modified, "blocked": s.blocked,
                   "errors": s.errors, "block_rate": round(s.block_rate, 3),
                   "avg_latency_ms": round(s.avg_latency_ms, 1), "kb": round(s.bytes / 1024, 1),
                   "tripped": s.tripped}
               for d, s in sorted(gate.stats.items())}
    summary = {
        "companies": len(companies),
        "succeeded": sum(1 for r in results if not r.error),
        "jobs_seen": sum(r.jobs for r in results),
        "new": sum(r.new for r in results), "updated": sum(r.updated for r in results),
        "closed": sum(r.closed for r in results),
        "seconds": round(time.perf_counter() - t0, 2),
        "results": [asdict(r) for r in results],
        "domains": domains,
    }
    store.finish_run(run_id, summary)
    store.close()
    return summary
