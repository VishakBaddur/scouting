"""Fallback for career pages that aren't on a known ATS.

Escalation ladder (cheapest first):
  1. Plain HTTP GET of the page -> look for an embedded ATS (iframe/script/link)
  2. Parse schema.org JobPosting JSON-LD (Google for Jobs markup) from that HTML
  3. Only if both fail: render with headless Chromium (Playwright), then
     a) re-run ATS detection + JSON-LD on the rendered DOM
     b) use JSON the page itself fetched (network sniffing) - most JS career
        sites load jobs from an internal JSON endpoint, which is far more
        reliable to parse than the rendered HTML

While rendering we block images, fonts and media: they are most of the bytes
and none of the data, so this cuts bandwidth (and proxy cost) a lot.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..detect import Board, detect_from_html
from ..fetch import Fetcher
from ..models import Job, clean_text

BLOCKED_RESOURCES = {"image", "font", "media", "stylesheet"}


def _iter_jsonld(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _iter_jsonld(x)
    elif isinstance(obj, dict):
        if "@graph" in obj:
            yield from _iter_jsonld(obj["@graph"])
        yield obj


def _location(loc) -> str:
    if isinstance(loc, list):
        return "; ".join(filter(None, (_location(x) for x in loc)))
    if isinstance(loc, dict):
        addr = loc.get("address", loc)
        if isinstance(addr, dict):
            parts = (addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry"))
            return ", ".join(p if isinstance(p, str) else (p or {}).get("name", "") for p in parts if p)
    return loc if isinstance(loc, str) else ""


def extract_jsonld_jobs(html: str, company: str, base_url: str) -> list[Job]:
    tree = HTMLParser(html)
    jobs = []
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(strip=False) or "null")
        except json.JSONDecodeError:
            continue
        for item in _iter_jsonld(data):
            types = item.get("@type")
            types = types if isinstance(types, list) else [types]
            if "JobPosting" not in types:
                continue
            ident = item.get("identifier")
            ext_id = (ident.get("value") if isinstance(ident, dict) else ident) or item.get("url") or item.get("title")
            sal = item.get("baseSalary") or {}
            val = sal.get("value") if isinstance(sal, dict) else None
            salary = ""
            if isinstance(val, dict) and (val.get("minValue") or val.get("value")):
                salary = f"{sal.get('currency', '')} {val.get('minValue', val.get('value'))}-{val.get('maxValue', '')}".strip()
            jobs.append(Job(
                company=company, source="jsonld", external_id=str(ext_id), title=item.get("title", ""),
                url=urljoin(base_url, item.get("url") or base_url),
                location=_location(item.get("jobLocation")),
                remote=(item.get("jobLocationType") == "TELECOMMUTE") or None,
                employment_type=item.get("employmentType", "") if isinstance(item.get("employmentType"), str) else "",
                posted_at=item.get("datePosted"), description=clean_text(item.get("description")),
                salary=salary, raw=item,
            ))
    return jobs


_JOB_PATH = re.compile(r"/(?:careers|jobs|job|positions|openings|roles|opportunities)/([A-Za-z0-9][A-Za-z0-9_-]{2,})/?$", re.I)
_NOT_JOBS = {"search", "apply", "all", "benefits", "culture", "faq", "students", "university", "teams", "locations"}


def extract_job_links(html: str, company: str, base_url: str) -> list[Job]:
    """Last-resort heuristic for hand-built career pages: same-site links like /careers/<slug>.

    Title comes from a heading inside the link (or the link text).
    """
    tree = HTMLParser(html)
    base_host = urlparse(base_url).netloc
    base_path = urlparse(base_url).path.rstrip("/")
    jobs, seen = [], set()
    for a in tree.css("a[href]"):
        url = urljoin(base_url, a.attributes.get("href") or "")
        parts = urlparse(url)
        if parts.netloc != base_host or parts.path.rstrip("/") == base_path:
            continue
        m = _JOB_PATH.search(parts.path)
        if not m or m.group(1).lower() in _NOT_JOBS:
            continue
        heading = a.css_first("h1, h2, h3, h4, h5, strong")
        title = clean_text((heading or a).text(separator=" "))
        if not (3 <= len(title) <= 120) or url in seen:
            continue
        seen.add(url)
        jobs.append(Job(company=company, source="links", external_id=m.group(1), title=title, url=url))
    return jobs


def jobs_from_sniffed_json(payloads: list[tuple[str, object]], company: str, base_url: str) -> list[Job]:
    """Heuristic: find arrays of objects that look like job postings in JSON the page loaded."""
    jobs = []

    def walk(obj, src):
        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
            keys = set().union(*(x.keys() for x in obj[:5]))
            title_key = next((k for k in ("title", "jobTitle", "name", "text") if k in keys), None)
            url_key = next((k for k in ("url", "absolute_url", "jobUrl", "applyUrl", "hostedUrl", "link") if k in keys), None)
            if title_key and (url_key or "id" in keys):
                for x in obj:
                    loc = x.get("location")
                    jobs.append(Job(
                        company=company, source="sniffed",
                        external_id=str(x.get("id") or x.get(url_key) or x.get(title_key)),
                        title=str(x.get(title_key, "")), url=urljoin(base_url, str(x.get(url_key) or base_url)),
                        location=loc if isinstance(loc, str) else (loc or {}).get("name", "") if isinstance(loc, dict) else "",
                        raw={"_source_url": src, **x},
                    ))
                return
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v, src)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, src)

    for src, payload in payloads:
        walk(payload, src)
    return jobs


async def render(url: str, timeout_ms: int = 30000, proxy: str | None = None) -> tuple[str, list[tuple[str, object]]]:
    """Render a page in headless Chromium; return final HTML and JSON responses it loaded."""
    from playwright.async_api import async_playwright

    captured: list[tuple[str, object]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
        ctx = await browser.new_context(viewport={"width": 1366, "height": 900}, locale="en-US")
        page = await ctx.new_page()

        async def block(route):
            if route.request.resource_type in BLOCKED_RESOURCES:
                await route.abort()
            else:
                await route.continue_()

        async def on_response(resp):
            if "json" in (resp.headers.get("content-type") or ""):
                try:
                    captured.append((resp.url, await resp.json()))
                except Exception:
                    pass

        await page.route("**/*", block)
        page.on("response", on_response)
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        html = await page.content()
        await browser.close()
    return html, captured


async def scrape_career_page(fetcher: Fetcher, url: str, company: str, use_browser: bool = True,
                             proxy: str | None = None) -> tuple[Board | None, list[Job], str]:
    """Return (ats_board_if_found, jobs, method_used)."""
    resp = await fetcher.get(url, check_robots=True)
    board = detect_from_html(resp.text, url)
    if board:
        return board, [], "static-html:ats"
    jobs = extract_jsonld_jobs(resp.text, company, url)
    if jobs:
        return None, jobs, "static-html:jsonld"
    links = extract_job_links(resp.text, company, url)
    if links:
        return None, links, "static-html:links"
    if not use_browser:
        return None, [], "static-html:none"

    html, payloads = await render(url, proxy=proxy)
    board = detect_from_html(html, url)
    if board:
        return board, [], "browser:ats"
    jobs = extract_jsonld_jobs(html, company, url)
    if jobs:
        return None, jobs, "browser:jsonld"
    jobs = jobs_from_sniffed_json(payloads, company, url)
    if jobs:
        return None, jobs, "browser:sniffed-json"
    jobs = extract_job_links(html, company, url)
    return None, jobs, "browser:links" if jobs else "browser:none"
