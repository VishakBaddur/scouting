"""End-to-end test of the career-page fallback against a local site (real Chromium, no internet)."""
import functools
import http.server
import threading
from pathlib import Path

import pytest

from jobscout.adapters.browser import scrape_career_page
from jobscout.fetch import Fetcher
from jobscout.politeness import DomainGate, PolitenessConfig

SITE = Path(__file__).parent / "site"


@pytest.fixture(scope="module")
def site_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SITE))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.mark.parametrize("page,method,titles", [
    ("static.html", "static-html:jsonld", ["Backend Engineer"]),
    ("careers.html", "static-html:links", ["Backend Engineer", "Full Stack Engineer"]),
    ("index.html", "browser:sniffed-json", ["Platform Engineer", "Data Engineer"]),
])
async def test_career_page_fallbacks(site_url, page, method, titles):
    f = Fetcher(DomainGate(PolitenessConfig(min_interval_s=0, jitter_s=0)))
    try:
        board, jobs, how = await scrape_career_page(f, f"{site_url}/{page}", "Acme")
    finally:
        await f.aclose()
    assert board is None and how == method
    assert [j.title for j in jobs] == titles
