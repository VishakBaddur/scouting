"""Figure out which applicant-tracking system (ATS) powers a careers page.

Most company career pages are thin wrappers around an ATS that exposes a
public JSON API. Hitting that API is faster, cheaper and far less likely to
be blocked than rendering the page, so detection is the first step of every
crawl. Detection works on the URL first and falls back to scanning HTML
(embedded iframes, script tags and links reveal the ATS board token).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# (source, regex) - each regex captures the board token / tenant info.
_URL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    # Workday: https://{tenant}.wd{N}.myworkdayjobs.com/{locale?}/{site}
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
]

# Greenhouse embeds on a company's own domain use ?for=<token>
_GH_FOR = re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class Board:
    source: str
    token: str               # board token, or "tenant|wdN|site" for workday
    careers_url: str = ""

    @property
    def workday_parts(self) -> tuple[str, str, str]:
        tenant, wd, site = self.token.split("|")
        return tenant, wd, site


def detect_from_url(url: str) -> Board | None:
    for source, pat in _URL_PATTERNS:
        m = pat.search(url)
        if not m:
            continue
        if source == "workday":
            return Board("workday", f"{m.group(1)}|{m.group(2)}|{m.group(3)}", url)
        return Board(source, m.group(1), url)
    return None


def detect_from_html(html: str, careers_url: str = "") -> Board | None:
    """Scan page HTML for links, iframes or scripts pointing at a known ATS."""
    m = _GH_FOR.search(html)
    if m:
        return Board("greenhouse", m.group(1), careers_url)
    for source, pat in _URL_PATTERNS:
        for m in pat.finditer(html):
            if source == "workday":
                return Board("workday", f"{m.group(1)}|{m.group(2)}|{m.group(3)}", careers_url)
            token = m.group(1)
            # ignore asset paths like jobs.lever.co/static/...
            if token.lower() in {"static", "assets", "embed", "js", "css"}:
                continue
            return Board(source, token, careers_url)
    return None
