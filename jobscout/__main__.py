"""Command line: python -m jobscout {crawl,search,stats}"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from .crawl import crawl, load_companies
from .politeness import PolitenessConfig
from .store import Store


def print_summary(s: dict) -> None:
    print(f"\nCrawled {s['succeeded']}/{s['companies']} companies in {s['seconds']}s | "
          f"jobs seen {s['jobs_seen']} | new {s['new']} | updated {s['updated']} | closed {s['closed']}\n")
    print(f"{'company':<22}{'method':<34}{'jobs':>6}{'new':>6}{'upd':>6}{'closed':>8}{'sec':>7}  error")
    for r in s["results"]:
        print(f"{r['company'][:21]:<22}{r['method'][:33]:<34}{r['jobs']:>6}{r['new']:>6}{r['updated']:>6}"
              f"{r['closed']:>8}{r['seconds']:>7}  {r['error']}")
    print(f"\n{'domain':<40}{'req':>5}{'ok':>5}{'304':>5}{'blk':>5}{'blk%':>7}{'avg ms':>9}{'KB':>9}")
    for d, v in s["domains"].items():
        print(f"{d[:39]:<40}{v['requests']:>5}{v['ok']:>5}{v['not_modified']:>5}{v['blocked']:>5}"
              f"{v['block_rate'] * 100:>6.0f}%{v['avg_latency_ms']:>9}{v['kb']:>9}"
              + ("  TRIPPED" if v["tripped"] else ""))


def main() -> None:
    p = argparse.ArgumentParser(prog="jobscout", description="Crawl company job boards into SQLite.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="crawl companies from a YAML file")
    c.add_argument("companies", help="YAML file with a 'companies' list")
    c.add_argument("--db", default="jobs.db")
    c.add_argument("--concurrency", type=int, default=8)
    c.add_argument("--no-browser", action="store_true", help="never launch Chromium")
    c.add_argument("--proxy", default=os.getenv("JOBSCOUT_PROXY"), help="proxy URL used only for domains that block us")
    c.add_argument("--min-interval", type=float, default=1.0, help="seconds between requests to the same domain")
    c.add_argument("--json", action="store_true", help="print the raw JSON summary")

    s = sub.add_parser("search", help="keyword search over open jobs")
    s.add_argument("query")
    s.add_argument("--db", default="jobs.db")
    s.add_argument("--limit", type=int, default=20)

    st = sub.add_parser("stats", help="job counts and last run")
    st.add_argument("--db", default="jobs.db")

    args = p.parse_args()
    if args.cmd == "crawl":
        cfg = PolitenessConfig(min_interval_s=args.min_interval)
        summary = asyncio.run(crawl(load_companies(args.companies), args.db, args.concurrency,
                                    use_browser=not args.no_browser, proxy=args.proxy, cfg=cfg))
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print_summary(summary)
    elif args.cmd == "search":
        store = Store(args.db)
        for r in store.search(args.query, args.limit):
            print(f"{r['company']:<20} {r['title'][:55]:<56} {(r['location'] or '')[:28]:<29} {r['url']}")
    elif args.cmd == "stats":
        store = Store(args.db)
        print(store.counts())
        row = store.conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            sm = json.loads(row["summary"] or "{}")
            print(f"last run {row['started_at']} -> {row['finished_at']}: "
                  f"{sm.get('succeeded')}/{sm.get('companies')} companies, {sm.get('jobs_seen')} jobs")


if __name__ == "__main__":
    main()
