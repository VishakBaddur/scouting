# jobscout

A polite, cost-aware crawler that pulls job postings from company career pages into SQLite,
with change detection for daily re-crawls.

It prefers each job board's public JSON API, falls back to structured data in the HTML, and only
launches a headless browser when nothing cheaper works.

## Results (measured on my laptop, 2026-10-14)

| | |
|---|---|
| Companies crawled | 12 across Greenhouse, Lever, Ashby, SmartRecruiters, Workday + 1 hand-built career page |
| Jobs collected | **5,027** |
| First crawl without NVIDIA's full Workday pagination | 3,065 jobs in **4.2 s** |
| Daily re-crawl of the API-based boards | **0 KB** downloaded (all `304 Not Modified`), down from ~31 MB |
| NVIDIA (Workday, 2,000 jobs over 100 pages) | **132 s → 68 s** after concurrent pagination, 0 blocks |
| Change detection on live data | Picked up 1 new + 1 closed Pinterest posting between runs |
| Tests | 26 passing (offline; HTTP mocked, browser tests use a local site) |

## How it works

For each company, the cheapest method that works wins:

1. **Detect the ATS from the URL** (Greenhouse, Lever, Ashby, SmartRecruiters, Workday) and call its public JSON API.
2. Otherwise, **fetch the page with plain HTTP** and look for:
   - an embedded ATS (iframe, script or link) → use that ATS's API
   - schema.org `JobPosting` JSON-LD
   - links shaped like `/careers/<slug>` (hand-built career pages)
3. Only if all of that fails, **render with headless Chromium** (images, fonts and CSS blocked), then:
   - re-check for an ATS and JSON-LD in the rendered DOM
   - **sniff the JSON the page itself fetched**, which is usually its internal jobs API
4. **Store in SQLite** with a content hash, so each run reports jobs as new / updated / unchanged / closed.

Most "JavaScript-heavy" career sites are thin wrappers around an ATS with a public JSON endpoint.
Workday is the clearest example: the site is a JS app, but it loads data from a
`POST /wday/cxs/{tenant}/{site}/jobs` endpoint you can call directly.

**Politeness and blocking** (`politeness.py`, `fetch.py`)
- Per-domain concurrency cap and minimum spacing with jitter; public ATS APIs run faster than company sites.
- Exponential backoff with full jitter on 429/5xx, honouring `Retry-After`.
- Detects challenge pages served with HTTP 200 (Cloudflare, PerimeterX, Imperva markers).
- Circuit breaker: after 5 consecutive blocks a domain is skipped for the rest of the run instead of burning requests.
- Proxy escalation: a domain moves to a proxy only once its measured block rate passes 30%, because proxies cost money per GB.
- Respects `robots.txt` for company HTML pages.

**Cost and latency**
- Conditional GETs (`ETag` / `If-None-Match`, `Last-Modified`), persisted across runs, so unchanged boards cost almost nothing.
- Browser runs block images, fonts, media and stylesheets.
- Per-domain metrics after every run: requests, 304s, blocks, block rate, average latency, KB transferred.

**Correctness**
- `content_hash` over title, location, description, salary and employment type drives new / updated / unchanged.
- Jobs missing from a company's feed are marked `closed_at`, and reopened if they come back.
- A company that returns zero jobs never closes its existing postings (protects against a broken scrape wiping data).
- One failing company never stops the run.

## Usage

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

python -m jobscout crawl companies.example.yaml          # crawl
python -m jobscout search "new grad backend engineer"     # keyword search, title matches ranked first
python -m jobscout stats
python -m pytest                                          # 26 tests, no network needed
```

Options: `--concurrency`, `--min-interval`, `--no-browser`, `--proxy URL` (or `JOBSCOUT_PROXY`), `--json`.

`companies.yaml` entries need a `name` plus either a `url` (ATS auto-detected) or `ats` + `token`.

## Known limitations / next steps

- **Workday caps pagination at ~2,000 results.** The fix is to split the query by Workday facets (location, job family) so each slice stays under the cap.
- **Search is keyword-based (SQLite `LIKE`).** Next step is Postgres with full-text search plus `pgvector` embeddings for natural-language queries, tuned with `EXPLAIN ANALYZE` against latency and cost.
- **No CAPTCHA solving or browser fingerprint spoofing.** The design avoids triggering defenses (APIs first, pacing, backoff) rather than defeating them.
- Proxy escalation is unit-tested but hasn't been exercised against a real proxy provider.
- The `/careers/<slug>` link heuristic is a last resort and can miss unusual URL schemes.

## Layout

| File | Purpose |
|---|---|
| `jobscout/detect.py` | ATS detection from URLs and HTML |
| `jobscout/politeness.py` | Per-domain spacing, backoff, circuit breaker, block stats |
| `jobscout/fetch.py` | httpx client: retries, conditional GETs, proxy escalation, robots.txt |
| `jobscout/adapters/ats.py` | Greenhouse, Lever, Ashby, SmartRecruiters, Workday |
| `jobscout/adapters/browser.py` | JSON-LD, link heuristic, Playwright render + JSON sniffing |
| `jobscout/store.py` | SQLite with change detection and HTTP cache |
| `jobscout/crawl.py` | Concurrent orchestrator + run metrics |
| `jobscout/__main__.py` | CLI |
| `tests/` | Offline tests + local test site |
