"""SQLite: every posting the bot has seen and what it decided, plus a little bookkeeping."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .matching import normalize
from .models import Job, Outcome, StoredJob

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT NOT NULL,
    url             TEXT NOT NULL,
    date_posted     TEXT,
    is_remote       INTEGER NOT NULL DEFAULT 0,
    fingerprint     TEXT NOT NULL,
    status          TEXT NOT NULL,
    title_match     INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    tracks          TEXT NOT NULL DEFAULT '',
    flags           TEXT NOT NULL DEFAULT '',
    starred         INTEGER NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL DEFAULT '',
    unverified      INTEGER NOT NULL DEFAULT 0,
    employment_type TEXT,
    seniority       TEXT,
    first_seen      TEXT NOT NULL,
    notified_at     TEXT,
    applied_at      TEXT,
    message_id      INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs (fingerprint);

CREATE TABLE IF NOT EXISTS queries (
    key     TEXT PRIMARY KEY,
    last_ok TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(job: Job) -> str:
    """Same title, company and place = the same opening, even under a new LinkedIn id."""
    return " | ".join(normalize(part) for part in (job.title, job.company, job.location))


class Storage:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ── jobs ────────────────────────────────────────────────────────────────

    def known_ids(self, ids: Iterable[str]) -> set[str]:
        ids = list(ids)
        known: set[str] = set()
        for start in range(0, len(ids), 500):  # stay under SQLite's variable limit
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._db.execute(f"SELECT id FROM jobs WHERE id IN ({marks})", chunk)
            known.update(row["id"] for row in rows)
        return known

    def add(self, job: Job, status: str, *, reason: str = "", title_match: bool = False) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO jobs
               (id, title, company, location, url, date_posted, is_remote, fingerprint,
                status, title_match, reason, first_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.id,
                job.title,
                job.company,
                job.location,
                job.url,
                job.date_posted.isoformat() if job.date_posted else None,
                int(job.is_remote),
                fingerprint(job),
                status,
                int(title_match),
                reason,
                _now(),
            ),
        )
        self._db.commit()

    def get(self, job_id: str) -> StoredJob | None:
        row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _stored(row) if row else None

    def pending(self) -> list[StoredJob]:
        """Jobs waiting on their description: sure matches first, then oldest first."""
        rows = self._db.execute("SELECT * FROM jobs WHERE status = 'pending' ORDER BY title_match DESC, first_seen, id")
        return [_stored(row) for row in rows]

    def matched(self) -> list[StoredJob]:
        """Accepted jobs that haven't been sent yet, oldest posting first."""
        rows = self._db.execute(
            "SELECT * FROM jobs WHERE status = 'matched' ORDER BY COALESCE(date_posted, ''), first_seen, id"
        )
        return [_stored(row) for row in rows]

    def with_status(self, status: str) -> list[StoredJob]:
        rows = self._db.execute("SELECT * FROM jobs WHERE status = ? ORDER BY first_seen, id", (status,))
        return [_stored(row) for row in rows]

    def bump_attempts(self, job_id: str) -> int:
        self._db.execute("UPDATE jobs SET attempts = attempts + 1 WHERE id = ?", (job_id,))
        self._db.commit()
        row = self._db.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return int(row["attempts"]) if row else 0

    def resolve(
        self,
        job_id: str,
        status: str,
        outcome: Outcome,
        *,
        employment_type: str | None = None,
        seniority: str | None = None,
    ) -> None:
        """Record the final verdict on a pending job."""
        reason = "; ".join(match.reason for match in outcome.matches) or outcome.reason
        self._db.execute(
            """UPDATE jobs SET status = ?, tracks = ?, flags = ?, starred = ?, reason = ?,
                               unverified = ?, employment_type = ?, seniority = ?
               WHERE id = ?""",
            (
                status,
                ",".join(outcome.track_ids),
                ",".join(outcome.flags),
                int(outcome.starred),
                reason,
                int(outcome.unverified),
                employment_type,
                seniority,
                job_id,
            ),
        )
        self._db.commit()

    def is_repost(self, job: Job, within_days: int) -> bool:
        """Was the same opening already sent recently (or is it queued) under another id?"""
        if within_days <= 0:
            return False
        since = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat(timespec="seconds")
        row = self._db.execute(
            """SELECT 1 FROM jobs
               WHERE fingerprint = ? AND id != ?
                 AND (status = 'matched' OR (notified_at IS NOT NULL AND notified_at >= ?))
               LIMIT 1""",
            (fingerprint(job), job.id, since),
        ).fetchone()
        return row is not None

    def mark_sent(self, job_id: str, message_id: int | None) -> None:
        self._db.execute(
            "UPDATE jobs SET status = 'sent', notified_at = ?, message_id = ? WHERE id = ?",
            (_now(), message_id, job_id),
        )
        self._db.commit()

    def set_applied(self, job_id: str, applied: bool) -> None:
        self._db.execute("UPDATE jobs SET applied_at = ? WHERE id = ?", (_now() if applied else None, job_id))
        self._db.commit()

    def mark_dismissed(self, job_id: str) -> None:
        self._db.execute("UPDATE jobs SET status = 'dismissed' WHERE id = ?", (job_id,))
        self._db.commit()

    def applied(self, limit: int = 100) -> list[StoredJob]:
        rows = self._db.execute(
            "SELECT * FROM jobs WHERE applied_at IS NOT NULL ORDER BY applied_at DESC LIMIT ?", (limit,)
        )
        return [_stored(row) for row in rows]

    def counts(self) -> dict[str, int]:
        counts = {
            row["status"]: row["n"]
            for row in self._db.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        }
        counts["applied"] = self._db.execute("SELECT COUNT(*) AS n FROM jobs WHERE applied_at IS NOT NULL").fetchone()[
            "n"
        ]
        return counts

    def prune(self, keep_days: int) -> int:
        """Forget old postings. Applied ones are kept, as are ones still waiting to be sent."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")
        cursor = self._db.execute(
            """DELETE FROM jobs
               WHERE first_seen < ? AND applied_at IS NULL AND status NOT IN ('pending', 'matched')""",
            (cutoff,),
        )
        self._db.commit()
        return cursor.rowcount

    # ── search bookkeeping ──────────────────────────────────────────────────

    def query_last_ok(self, key: str) -> datetime | None:
        row = self._db.execute("SELECT last_ok FROM queries WHERE key = ?", (key,)).fetchone()
        return datetime.fromisoformat(row["last_ok"]) if row else None

    def set_query_ok(self, key: str, when: datetime) -> None:
        self._db.execute(
            "INSERT INTO queries (key, last_ok) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET last_ok = excluded.last_ok",
            (key, when.astimezone(timezone.utc).isoformat(timespec="seconds")),
        )
        self._db.commit()

    # ── key/value ───────────────────────────────────────────────────────────

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()


def _stored(row: sqlite3.Row) -> StoredJob:
    return StoredJob(
        job=Job(
            id=row["id"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            url=row["url"],
            date_posted=date.fromisoformat(row["date_posted"]) if row["date_posted"] else None,
            is_remote=bool(row["is_remote"]),
        ),
        status=row["status"],
        title_match=bool(row["title_match"]),
        attempts=row["attempts"],
        tracks=tuple(filter(None, row["tracks"].split(","))),
        flags=tuple(filter(None, row["flags"].split(","))),
        starred=bool(row["starred"]),
        reason=row["reason"],
        unverified=bool(row["unverified"]),
        employment_type=row["employment_type"],
        seniority=row["seniority"],
        applied=row["applied_at"] is not None,
        message_id=row["message_id"],
        notified_at=row["notified_at"],
    )
