"""Adapters for public ATS job-board APIs.

Each adapter turns one provider's JSON into normalized Job objects. These
endpoints are the ones the providers' own embeddable job boards call, so
they are stable, cheap and rarely bot-protected - always try them before
rendering HTML.
"""
from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone

from ..detect import Board
from ..fetch import Fetcher
from ..models import Job, clean_text

MAX_PAGES = 50  # safety cap for paginated APIs
WORKDAY_MAX_RESULTS = 2000  # Workday stops paging around here; split by facets to go deeper


def _ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds")


async def greenhouse(fetcher: Fetcher, board: Board, company: str) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board.token}/jobs?content=true"
    data = (await fetcher.get(url)).json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(Job(
            company=company, source="greenhouse", external_id=str(j["id"]),
            title=j.get("title", ""), url=j.get("absolute_url", ""),
            location=(j.get("location") or {}).get("name", ""),
            department=", ".join(d.get("name", "") for d in j.get("departments") or []),
            posted_at=j.get("updated_at"),
            description=clean_text(html.unescape(j.get("content") or "")),
            raw=j,
        ))
    return jobs


async def lever(fetcher: Fetcher, board: Board, company: str) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{board.token}?mode=json"
    data = (await fetcher.get(url)).json()
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        sal = j.get("salaryRange") or {}
        salary = (f"{sal.get('currency', '')} {sal.get('min')}-{sal.get('max')} / {sal.get('interval', '')}".strip()
                  if sal.get("min") else "")
        jobs.append(Job(
            company=company, source="lever", external_id=j["id"], title=j.get("text", ""),
            url=j.get("hostedUrl", ""), location=cats.get("location", "") or "",
            remote=(j.get("workplaceType") == "remote") if j.get("workplaceType") else None,
            department=cats.get("team", "") or "", employment_type=cats.get("commitment", "") or "",
            posted_at=_ms_to_iso(j.get("createdAt")),
            description=j.get("descriptionPlain", "") or "", salary=salary, raw=j,
        ))
    return jobs


async def ashby(fetcher: Fetcher, board: Board, company: str) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board.token}?includeCompensation=true"
    data = (await fetcher.get(url)).json()
    jobs = []
    for j in data.get("jobs", []):
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        jobs.append(Job(
            company=company, source="ashby", external_id=j["id"], title=j.get("title", ""),
            url=j.get("jobUrl", ""), location=j.get("location", "") or "",
            remote=j.get("isRemote"), department=j.get("department", "") or "",
            employment_type=j.get("employmentType", "") or "", posted_at=j.get("publishedAt"),
            description=j.get("descriptionPlain", "") or "", salary=comp, raw=j,
        ))
    return jobs


async def smartrecruiters(fetcher: Fetcher, board: Board, company: str) -> list[Job]:
    jobs, offset, limit = [], 0, 100
    for _ in range(MAX_PAGES):
        url = f"https://api.smartrecruiters.com/v1/companies/{board.token}/postings?limit={limit}&offset={offset}"
        data = (await fetcher.get(url)).json()
        content = data.get("content", [])
        for j in content:
            loc = j.get("location") or {}
            jobs.append(Job(
                company=company, source="smartrecruiters", external_id=str(j["id"]),
                title=j.get("name", ""), url=f"https://jobs.smartrecruiters.com/{board.token}/{j['id']}",
                location=", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x),
                remote=loc.get("remote"), department=(j.get("department") or {}).get("label", "") or "",
                employment_type=(j.get("typeOfEmployment") or {}).get("label", "") or "",
                posted_at=j.get("releasedDate"), raw=j,
            ))
        offset += limit
        if not content or offset >= data.get("totalFound", 0):
            break
    return jobs


def _workday_jobs(data: dict, host: str, site: str, company: str) -> list[Job]:
    jobs = []
    for j in data.get("jobPostings", []):
        path = j.get("externalPath", "")
        jobs.append(Job(
            company=company, source="workday",
            external_id=(j.get("bulletFields") or [path])[0] or path,
            title=j.get("title", ""), url=f"{host}/{site}{path}",
            location=j.get("locationsText", "") or "", posted_at=j.get("postedOn"), raw=j,
        ))
    return jobs


async def workday(fetcher: Fetcher, board: Board, company: str) -> list[Job]:
    """Workday career sites are JS apps backed by a JSON 'cxs' endpoint (POST, paginated).

    Page 1 tells us the total (later pages report 0), so the remaining pages are
    requested concurrently. The per-domain gate still caps concurrency and spacing,
    so this overlaps network latency without hitting Workday any harder.
    """
    tenant, wd, site = board.workday_parts
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    limit = 20

    async def page(offset: int) -> dict:
        return (await fetcher.post_json(api, {"appliedFacets": {}, "limit": limit, "offset": offset,
                                               "searchText": ""})).json()

    first = await page(0)
    total = min(first.get("total", 0), WORKDAY_MAX_RESULTS)
    jobs = _workday_jobs(first, host, site, company)
    offsets = list(range(limit, total, limit))
    for data in await asyncio.gather(*(page(o) for o in offsets)):
        jobs.extend(_workday_jobs(data, host, site, company))
    return jobs


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workday": workday,
}
