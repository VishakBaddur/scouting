"""SQLite storage with change detection.

Every crawl answers "what changed?" rather than "what exists?":
  * new        - first time we've seen this (company, source, external_id)
  * updated    - seen before, but the content hash moved (title/desc/salary/location changed)
  * unchanged  - only last_seen is bumped (cheap)
  * closed     - previously open job for this company that didn't appear this run

The HTTP cache (ETag / Last-Modified + body) is persisted too, so conditional
requests keep working across daily runs.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .models import Job, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    location TEXT,
    remote INTEGER,
    department TEXT,
    employment_type TEXT,
    posted_at TEXT,
    description TEXT,
    salary TEXT,
    content_hash TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_company_open ON jobs(company, closed_at);
CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title);

CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    body TEXT
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT
);
"""

COLUMNS = ["key", "company", "source", "external_id", "title", "url", "location", "remote", "department",
           "employment_type", "posted_at", "description", "salary", "content_hash"]


@dataclass
class UpsertResult:
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    closed: int = 0


class Store:
    def __init__(self, path: str = "jobs.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    def upsert_company_jobs(self, company: str, jobs: list[Job], now: str | None = None) -> UpsertResult:
        now = now or utcnow()
        res = UpsertResult()
        existing = {r["key"]: r["content_hash"] for r in
                    self.conn.execute("SELECT key, content_hash FROM jobs WHERE company = ?", (company,))}
        seen = set()
        with self.conn:
            for job in jobs:
                row = job.to_row()
                if row["key"] in seen:      # duplicate posting in the same feed
                    continue
                seen.add(row["key"])
                row["remote"] = None if row["remote"] is None else int(row["remote"])
                old_hash = existing.get(row["key"])
                if old_hash is None:
                    res.new += 1
                elif old_hash != row["content_hash"]:
                    res.updated += 1
                else:
                    res.unchanged += 1
                    self.conn.execute("UPDATE jobs SET last_seen = ?, closed_at = NULL WHERE key = ?", (now, row["key"]))
                    continue
                vals = [row[c] for c in COLUMNS]
                self.conn.execute(
                    f"""INSERT INTO jobs ({", ".join(COLUMNS)}, first_seen, last_seen, closed_at)
                        VALUES ({", ".join("?" for _ in COLUMNS)}, ?, ?, NULL)
                        ON CONFLICT(key) DO UPDATE SET
                          {", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c != "key")},
                          last_seen = excluded.last_seen, closed_at = NULL""",
                    vals + [now, now])
            gone = [k for k in existing if k not in seen]
            for k in gone:
                cur = self.conn.execute("UPDATE jobs SET closed_at = ? WHERE key = ? AND closed_at IS NULL", (now, k))
                res.closed += cur.rowcount
        return res

    def search(self, query: str, limit: int = 20, open_only: bool = True) -> list[sqlite3.Row]:
        """Keyword search: every word must appear in title, location or description; title matches rank first."""
        words = [w for w in query.lower().split() if w]
        where = ["closed_at IS NULL"] if open_only else []
        params: list = []
        for w in words:
            where.append("(lower(title) LIKE ? OR lower(location) LIKE ? OR lower(description) LIKE ?)")
            params += [f"%{w}%"] * 3
        sql = "SELECT company, title, location, salary, url FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if words:
            sql += " ORDER BY (" + " + ".join("(lower(title) LIKE ?)" for _ in words) + ") DESC, last_seen DESC"
            params += [f"%{w}%" for w in words]
        sql += " LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def counts(self) -> dict:
        r = self.conn.execute("SELECT COUNT(*) total, SUM(closed_at IS NULL) open FROM jobs").fetchone()
        return {"total": r["total"], "open": r["open"] or 0}

    def load_http_cache(self) -> dict:
        return {r["url"]: {"etag": r["etag"], "last_modified": r["last_modified"], "body": r["body"]}
                for r in self.conn.execute("SELECT * FROM http_cache")}

    def save_http_cache(self, cache: dict) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO http_cache (url, etag, last_modified, body) VALUES (?, ?, ?, ?)",
                [(u, v.get("etag"), v.get("last_modified"), v.get("body")) for u, v in cache.items()])

    def start_run(self) -> int:
        with self.conn:
            return self.conn.execute("INSERT INTO crawl_runs (started_at) VALUES (?)", (utcnow(),)).lastrowid

    def finish_run(self, run_id: int, summary: dict) -> None:
        with self.conn:
            self.conn.execute("UPDATE crawl_runs SET finished_at = ?, summary = ? WHERE id = ?",
                              (utcnow(), json.dumps(summary), run_id))
