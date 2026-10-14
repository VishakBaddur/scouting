"""Append one crawl summary (JSON from `jobscout crawl --json`) to RUNS.md and the GitHub step summary."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

summary = json.loads(Path(sys.argv[1]).read_text())
runs = Path(sys.argv[2] if len(sys.argv) > 2 else "RUNS.md")

d = summary["domains"].values()
req = sum(x["requests"] for x in d)
blocked = sum(x["blocked"] for x in d)
not_mod = sum(x["not_modified"] for x in d)
kb = sum(x["kb"] for x in d)
failed = [r["company"] for r in summary["results"] if r["error"]]

row = (f"| {datetime.now(timezone.utc):%Y-%m-%d %H:%M} | {summary['succeeded']}/{summary['companies']} "
       f"| {summary['jobs_seen']:,} | {summary['new']} | {summary['updated']} | {summary['closed']} "
       f"| {req} | {not_mod} | {blocked} | {kb / 1024:.1f} | {summary['seconds']} | {', '.join(failed) or '-'} |")

header = ("# Scheduled crawl log\n\nOne row per daily GitHub Actions run (`.github/workflows/crawl.yml`).\n\n"
          "| Run (UTC) | Companies OK | Jobs seen | New | Updated | Closed | Requests | 304s | Blocked | MB | Seconds | Failed |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|---|\n")
if not runs.exists():
    runs.write_text(header)
with runs.open("a") as f:
    f.write(row + "\n")

step = os.getenv("GITHUB_STEP_SUMMARY")
if step:
    with open(step, "a") as f:
        f.write(header.split("\n\n", 2)[2] + row + "\n\n")
        f.write("| Company | Method | Jobs | New | Closed | Seconds | Error |\n|---|---|---|---|---|---|---|\n")
        for r in summary["results"]:
            f.write(f"| {r['company']} | {r['method']} | {r['jobs']} | {r['new']} | {r['closed']} | {r['seconds']} | {r['error'] or '-'} |\n")
print(row)
