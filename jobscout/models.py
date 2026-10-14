"""Normalized job model shared by every adapter."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str:
    """Strip HTML tags and collapse whitespace."""
    if not value:
        return ""
    return _WS.sub(" ", _TAG.sub(" ", value)).strip()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    company: str
    source: str            # greenhouse | lever | ashby | smartrecruiters | workday | jsonld
    external_id: str       # id within the source; unique per (company, source)
    title: str
    url: str
    location: str = ""
    remote: bool | None = None
    department: str = ""
    employment_type: str = ""
    posted_at: str | None = None
    description: str = ""
    salary: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def key(self) -> str:
        return f"{self.company}:{self.source}:{self.external_id}"

    def content_hash(self) -> str:
        """Hash of the fields that matter, used for change detection between crawls."""
        payload = {
            "title": self.title.strip(),
            "location": self.location.strip(),
            "description": clean_text(self.description),
            "salary": self.salary.strip(),
            "employment_type": self.employment_type.strip(),
        }
        blob = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    def to_row(self) -> dict:
        row = asdict(self)
        row.pop("raw")
        row["key"] = self.key
        row["content_hash"] = self.content_hash()
        return row
