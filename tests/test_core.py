"""Offline tests: no network. HTTP is mocked with respx."""
import json

import httpx
import pytest
import respx

from jobscout.adapters.ats import ADAPTERS
from jobscout.adapters.browser import extract_job_links, extract_jsonld_jobs, jobs_from_sniffed_json
from jobscout.detect import detect_from_html, detect_from_url
from jobscout.fetch import BlockedError, DomainTrippedError, Fetcher
from jobscout.models import Job
from jobscout.politeness import DomainGate, PolitenessConfig, looks_like_block_page
from jobscout.store import Store

FAST = PolitenessConfig(min_interval_s=0, jitter_s=0, backoff_base_s=0.001, max_retries=3,
                        trip_after_consecutive_blocks=5)


@pytest.fixture
async def fetcher():
    f = Fetcher(DomainGate(FAST))
    yield f
    await f.aclose()


# ---------------- detection ----------------
@pytest.mark.parametrize("url,source,token", [
    ("https://job-boards.greenhouse.io/stripe/jobs/1", "greenhouse", "stripe"),
    ("https://boards.greenhouse.io/embed/job_board?for=acme", "greenhouse", "acme"),
    ("https://jobs.lever.co/palantir/abc", "lever", "palantir"),
    ("https://jobs.ashbyhq.com/notion", "ashby", "notion"),
    ("https://jobs.smartrecruiters.com/ServiceNow/123", "smartrecruiters", "ServiceNow"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite", "workday", "nvidia|wd5|NVIDIAExternalCareerSite"),
])
def test_detect_from_url(url, source, token):
    b = detect_from_url(url)
    assert (b.source, b.token) == (source, token)


def test_detect_from_html_skips_asset_paths():
    html = '<script src="https://jobs.lever.co/static/a.js"></script><a href="https://jobs.lever.co/acme">jobs</a>'
    assert detect_from_html(html).token == "acme"
    assert detect_from_html("<p>nothing here</p>") is None


# ---------------- politeness ----------------
async def test_requests_to_same_domain_are_spaced():
    clock = {"t": 0.0}
    slept = []

    async def fake_sleep(s):
        slept.append(s)
        clock["t"] += s

    gate = DomainGate(PolitenessConfig(min_interval_s=1.0, jitter_s=0), clock=lambda: clock["t"], sleep=fake_sleep)
    for _ in range(3):
        await gate.wait_turn("example.com")
    assert slept == [1.0, 1.0]


def test_backoff_honours_retry_after_and_caps():
    gate = DomainGate(PolitenessConfig(backoff_cap_s=60))
    assert gate.backoff_delay(0, "7") == 7.0
    assert gate.backoff_delay(0, "999") == 60
    assert 0 <= gate.backoff_delay(3) <= 8


def test_circuit_breaker_trips_after_consecutive_blocks():
    gate = DomainGate(PolitenessConfig(trip_after_consecutive_blocks=3))
    gate.record("x.com", 403, 1)
    gate.record("x.com", 200, 1)          # success resets the streak
    for _ in range(3):
        gate.record("x.com", 429, 1)
    assert gate.is_tripped("x.com")
    assert gate.stats["x.com"].block_rate == 4 / 5


def test_challenge_page_detection():
    assert looks_like_block_page(200, "<html>Please verify you are a human</html>")
    assert looks_like_block_page(403, "")
    assert not looks_like_block_page(200, "<html><h1>Careers</h1></html>")


# ---------------- fetcher ----------------
@respx.mock
async def test_retry_then_conditional_get(fetcher):
    route = respx.get("https://api.example.com/jobs").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"ok": 1}, headers={"ETag": '"v1"'}),
        httpx.Response(304),
    ])
    first = await fetcher.get("https://api.example.com/jobs")
    assert first.json() == {"ok": 1}
    second = await fetcher.get("https://api.example.com/jobs")
    assert second.from_cache and second.json() == {"ok": 1}
    assert route.calls[-1].request.headers["if-none-match"] == '"v1"'


@respx.mock
async def test_persistent_blocking_trips_domain(fetcher):
    respx.get("https://blocked.example.com/x").mock(return_value=httpx.Response(403))
    with pytest.raises((BlockedError, DomainTrippedError)):
        await fetcher.get("https://blocked.example.com/x")
    with pytest.raises(DomainTrippedError):
        await fetcher.get("https://blocked.example.com/x")


async def test_proxy_escalation_only_after_blocks():
    f = Fetcher(DomainGate(FAST), proxy_url="http://proxy.local:8080", proxy_min_requests=2, proxy_block_rate=0.5)
    assert f._client_for("site.com") is f._direct
    f.gate.record("site.com", 403, 1)
    f.gate.record("site.com", 403, 1)
    assert f._client_for("site.com") is f._proxied
    await f.aclose()


# ---------------- ATS adapters ----------------
@respx.mock
async def test_greenhouse_unescapes_html(fetcher):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").respond(json={"jobs": [
        {"id": 1, "title": "SWE, New Grad", "absolute_url": "https://x/1", "location": {"name": "NYC"},
         "content": "&lt;p&gt;Build &lt;b&gt;backend&lt;/b&gt;&lt;/p&gt;", "departments": [{"name": "Eng"}]}]})
    jobs = await ADAPTERS["greenhouse"](fetcher, detect_from_url("https://job-boards.greenhouse.io/acme"), "Acme")
    assert jobs[0].description == "Build backend" and jobs[0].department == "Eng"


@respx.mock
async def test_lever_salary_and_remote(fetcher):
    respx.get("https://api.lever.co/v0/postings/acme?mode=json").respond(json=[
        {"id": "a", "text": "Backend", "hostedUrl": "https://x", "categories": {"location": "Remote"},
         "workplaceType": "remote", "salaryRange": {"min": 130000, "max": 160000, "currency": "USD", "interval": "yr"}}])
    job = (await ADAPTERS["lever"](fetcher, detect_from_url("https://jobs.lever.co/acme"), "Acme"))[0]
    assert job.remote is True and "130000-160000" in job.salary


@respx.mock
async def test_smartrecruiters_paginates(fetcher):
    page = lambda ids, total: {"content": [{"id": i, "name": f"Job {i}"} for i in ids], "totalFound": total}
    respx.get("https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100&offset=0").respond(json=page(range(100), 150))
    respx.get("https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100&offset=100").respond(json=page(range(100, 150), 150))
    jobs = await ADAPTERS["smartrecruiters"](fetcher, detect_from_url("https://jobs.smartrecruiters.com/acme"), "Acme")
    assert len(jobs) == 150


@respx.mock
async def test_workday_uses_first_page_total(fetcher):
    """Workday reports the real total only on page 1; later pages say total=0."""
    def handler(request):
        off = json.loads(request.content)["offset"]
        n = min(20, 95 - off)
        return httpx.Response(200, json={"total": 95 if off == 0 else 0, "jobPostings": [
            {"title": f"SWE {off + i}", "externalPath": f"/job/{off + i}", "bulletFields": [f"JR{off + i}"]}
            for i in range(n)]})
    respx.post("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs").mock(side_effect=handler)
    jobs = await ADAPTERS["workday"](fetcher, detect_from_url("https://acme.wd5.myworkdayjobs.com/en-US/External"), "Acme")
    assert len(jobs) == 95 and len({j.external_id for j in jobs}) == 95


# ---------------- HTML fallbacks ----------------
JSONLD = """<html><head><script type="application/ld+json">{"@graph":[{"@type":"Organization"},
{"@type":"JobPosting","title":"Backend Engineer","identifier":{"value":"BE-1"},"url":"/jobs/be-1",
"jobLocation":{"address":{"addressLocality":"New York","addressRegion":"NY"}},
"baseSalary":{"currency":"USD","value":{"minValue":130000,"maxValue":160000}}}]}</script></head></html>"""


def test_jsonld_extraction():
    job = extract_jsonld_jobs(JSONLD, "Acme", "https://acme.com/careers")[0]
    assert (job.title, job.external_id, job.location) == ("Backend Engineer", "BE-1", "New York, NY")
    assert job.url == "https://acme.com/jobs/be-1" and "130000-160000" in job.salary


def test_job_links_use_headings_and_skip_nav():
    html = """<a href="/careers/benefits">Benefits</a>
              <a href="/careers/backend-engineer"><h3>Backend Engineer</h3><p>NYC</p></a>
              <a href="https://other.com/careers/x-y-z"><h3>Elsewhere</h3></a>"""
    jobs = extract_job_links(html, "Rotto", "https://rotto.com/careers")
    assert [(j.title, j.external_id) for j in jobs] == [("Backend Engineer", "backend-engineer")]


def test_sniffed_json_finds_job_arrays():
    payloads = [("https://acme.com/api/config", {"theme": "dark"}),
                ("https://acme.com/api/jobs", {"data": {"results": [
                    {"id": 1, "title": "Platform Engineer", "url": "/j/1", "location": "Remote"}]}})]
    jobs = jobs_from_sniffed_json(payloads, "Acme", "https://acme.com/careers")
    assert [(j.title, j.url) for j in jobs] == [("Platform Engineer", "https://acme.com/j/1")]


# ---------------- storage ----------------
def test_change_detection_across_runs():
    s = Store(":memory:")
    day1 = [Job("Acme", "gh", "1", "Backend", "u1"), Job("Acme", "gh", "2", "Data", "u2"), Job("Acme", "gh", "3", "SRE", "u3")]
    r1 = s.upsert_company_jobs("Acme", day1, "2026-10-13")
    assert (r1.new, r1.updated, r1.unchanged, r1.closed) == (3, 0, 0, 0)
    day2 = [Job("Acme", "gh", "1", "Backend", "u1"), Job("Acme", "gh", "2", "Data II", "u2"), Job("Acme", "gh", "4", "Infra", "u4")]
    r2 = s.upsert_company_jobs("Acme", day2, "2026-10-14")
    assert (r2.new, r2.updated, r2.unchanged, r2.closed) == (1, 1, 1, 1)
    assert s.counts() == {"total": 4, "open": 3}
    # a closed job that reappears is reopened
    r3 = s.upsert_company_jobs("Acme", day2 + [Job("Acme", "gh", "3", "SRE", "u3")], "2026-10-15")
    assert r3.unchanged == 4 and s.counts()["open"] == 4


def test_search_ranks_title_matches_first():
    s = Store(":memory:")
    s.upsert_company_jobs("Acme", [Job("Acme", "gh", "1", "Recruiter", "u1", description="hire backend engineers"),
                                   Job("Acme", "gh", "2", "Backend Engineer", "u2")])
    assert [r["title"] for r in s.search("backend")] == ["Backend Engineer", "Recruiter"]
