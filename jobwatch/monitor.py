"""One check: search LinkedIn, decide what's relevant, send it."""

from __future__ import annotations

import asyncio
import logging
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .config import Config
from .matching import Matcher, normalize
from .models import Decision, JobDetails, SearchResult, StoredJob
from .storage import Storage

log = logging.getLogger(__name__)

MAX_DETAIL_ATTEMPTS = 3  # tries at reading a description before deciding without it
STOP_AFTER_FAILURES = 3  # consecutive failed requests that end a phase early
ALERT_AFTER_FAILED_CYCLES = 3  # fully failed checks in a row before telling the user
SEND_PAUSE_SECONDS = 1.1  # Telegram allows about one message a second per chat


class NotifyError(Exception):
    """A message could not be delivered; the job stays queued for the next check."""


class Source(Protocol):
    details_available: bool

    def search(self, term: str, location: str, hours_old: int, results_wanted: int) -> SearchResult: ...

    def fetch_details(self, job_id: str) -> JobDetails | None: ...


class Notifier(Protocol):
    @property
    def ready(self) -> bool: ...

    async def send_job(self, stored: StoredJob) -> int | None: ...

    async def send_text(self, text: str) -> None: ...


@dataclass(frozen=True)
class Query:
    term: str
    location: str

    @property
    def key(self) -> str:
        return f"{normalize(self.term)}|{normalize(self.location)}"


@dataclass
class CycleReport:
    started: datetime
    finished: datetime | None = None
    queries: int = 0
    failed_queries: int = 0
    stopped_early: bool = False
    new: int = 0
    matched: int = 0
    reposts: int = 0
    waiting: int = 0  # still need their description read
    sent: int = 0
    queued: int = 0  # accepted but not delivered yet
    error: str = ""

    @property
    def blocked(self) -> bool:
        return self.queries > 0 and self.failed_queries == self.queries

    def summary(self) -> str:
        if self.blocked:
            return f"Every search failed ({self.error or 'no details'})."
        parts = [f"{self.queries - self.failed_queries}/{self.queries} searches ok", f"{self.new} new postings"]
        parts.append(f"{self.sent} sent")
        if self.reposts:
            parts.append(f"{self.reposts} reposts skipped")
        if self.waiting:
            parts.append(f"{self.waiting} waiting on their description")
        if self.queued:
            parts.append(f"{self.queued} queued to send")
        return ", ".join(parts) + "."


def build_queries(config: Config) -> list[Query]:
    """Every search term in every location, once: tracks may share a term."""
    queries: dict[str, Query] = {}
    for track in config.tracks:
        for term in track.search_terms:
            for location in config.locations:
                query = Query(term, location)
                queries.setdefault(query.key, query)
    return list(queries.values())


class Monitor:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        source: Source,
        notifier: Notifier,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.config = config
        self.storage = storage
        self.source = source
        self.notifier = notifier
        self.matcher = Matcher(config)
        self.queries = build_queries(config)
        self.last_report: CycleReport | None = None
        self._sleep = sleep

    async def run_cycle(self) -> CycleReport:
        report = CycleReport(started=datetime.now(timezone.utc))
        try:
            await self._search(report)
            await self._resolve_pending(report)
            await self._send_matched(report)
            self.storage.prune(self.config.keep_history_days)
        finally:
            report.finished = datetime.now(timezone.utc)
            self.last_report = report
        await self._report_health(report)
        log.info("Check finished: %s", report.summary())
        return report

    # ── 1. search ───────────────────────────────────────────────────────────

    async def _search(self, report: CycleReport) -> None:
        fresh = {}
        failures_in_a_row = 0
        for index, query in enumerate(self._rotated_queries()):
            if index:
                await self._pause(self.config.pause_between_queries)
            started = datetime.now(timezone.utc)
            hours = self._window_hours(query, started)
            result = await asyncio.to_thread(
                self.source.search, query.term, query.location, hours, self.config.results_per_query
            )
            report.queries += 1
            for job in result.jobs:
                fresh.setdefault(job.id, job)
            if result.ok:
                failures_in_a_row = 0
                self.storage.set_query_ok(query.key, started)
                log.info("%r in %s, last %dh: %d postings", query.term, query.location, hours, len(result.jobs))
            else:
                failures_in_a_row += 1
                report.failed_queries += 1
                report.error = result.error
                log.warning("%r in %s failed: %s", query.term, query.location, result.error)
                if failures_in_a_row >= STOP_AFTER_FAILURES:
                    # LinkedIn is throttling; more requests now only make the block last longer.
                    report.stopped_early = True
                    break

        known = self.storage.known_ids(fresh)
        for job in fresh.values():
            if job.id in known:
                continue
            report.new += 1
            outcome = self.matcher.evaluate(job)
            if outcome.decision is Decision.REJECT:
                self.storage.add(job, "rejected", reason=outcome.reason)
            else:
                self.storage.add(job, "pending", title_match=outcome.decision is Decision.ACCEPT)

    def _rotated_queries(self) -> list[Query]:
        """Start one query later each check, so a check that stops early never starves the same terms."""
        count = int(self.storage.get_meta("cycles", "0") or 0)
        self.storage.set_meta("cycles", str(count + 1))
        offset = count % len(self.queries) if self.queries else 0
        return self.queries[offset:] + self.queries[:offset]

    def _window_hours(self, query: Query, now: datetime) -> int:
        """
        How far back to ask LinkedIn for. Results come back by relevance, not date, so a
        short window is what keeps new postings from being buried: cover the time since
        this query last worked, plus an hour of overlap.
        """
        last_ok = self.storage.query_last_ok(query.key)
        if last_ok is None:
            return self.config.first_run_hours
        gap_hours = (now - last_ok).total_seconds() / 3600
        return max(2, min(self.config.max_lookback_hours, math.ceil(gap_hours) + 1))

    # ── 2. decide ───────────────────────────────────────────────────────────

    async def _resolve_pending(self, report: CycleReport) -> None:
        reading = self.config.read_descriptions and self.source.details_available
        budget = self.config.max_descriptions_per_cycle if reading else 0
        failures_in_a_row = 0

        for stored in self.storage.pending():
            job = stored.job
            details = None
            blocked = failures_in_a_row >= STOP_AFTER_FAILURES
            tried = False
            if reading and self.source.details_available and budget > 0 and not blocked:
                budget -= 1
                tried = True
                details = await asyncio.to_thread(self.source.fetch_details, job.id)
                await self._pause(self.config.pause_between_descriptions)
                failures_in_a_row = 0 if details is not None else failures_in_a_row + 1

            # A title match goes out now even without its description; it only misses the
            # extra tags. A job the description has to decide is worth waiting for: it is
            # retried on later checks, and judged without the description after
            # MAX_DETAIL_ATTEMPTS failures. Running out of this check's budget isn't a failure.
            if details is None and reading and self.source.details_available and not stored.title_match:
                failed = tried or blocked
                if not failed or self.storage.bump_attempts(job.id) < MAX_DETAIL_ATTEMPTS:
                    report.waiting += 1
                    continue

            outcome = self.matcher.evaluate(job, details, final=True)
            if outcome.decision is not Decision.ACCEPT:
                self.storage.resolve(job.id, "rejected", outcome)
                continue
            status = "duplicate" if self.storage.is_repost(job, self.config.repost_window_days) else "matched"
            self.storage.resolve(
                job.id,
                status,
                outcome,
                employment_type=details.employment_type if details else None,
                seniority=details.seniority if details else None,
            )
            if status == "matched":
                report.matched += 1
            else:
                report.reposts += 1

    # ── 3. send ─────────────────────────────────────────────────────────────

    async def _send_matched(self, report: CycleReport) -> None:
        queue = self.storage.matched()
        if not queue:
            return
        if not self.notifier.ready:
            report.queued = len(queue)
            log.info("%d jobs are waiting: send /start to the bot so it knows where to post.", len(queue))
            return
        for index, stored in enumerate(queue):
            if index:
                await self._sleep(SEND_PAUSE_SECONDS)
            try:
                message_id = await self.notifier.send_job(stored)
            except NotifyError as exc:
                report.queued = len(queue) - index
                log.warning("Sending stopped, %d jobs stay queued: %s", report.queued, exc)
                return
            self.storage.mark_sent(stored.job.id, message_id)
            report.sent += 1

    # ── health ──────────────────────────────────────────────────────────────

    async def _report_health(self, report: CycleReport) -> None:
        streak = int(self.storage.get_meta("failed_cycles", "0") or 0)
        if report.blocked:
            streak += 1
            self.storage.set_meta("failed_cycles", str(streak))
            if streak == ALERT_AFTER_FAILED_CYCLES:
                await self._tell(
                    f"⚠️ The last {streak} checks got nothing from LinkedIn "
                    f"({report.error or 'no details'}). I'll keep trying. If it goes on, check less "
                    "often or set PROXIES."
                )
        elif report.queries:
            if streak >= ALERT_AFTER_FAILED_CYCLES:
                await self._tell("✅ LinkedIn is answering again.")
            self.storage.set_meta("failed_cycles", "0")

    async def _tell(self, text: str) -> None:
        if not self.notifier.ready:
            return
        try:
            await self.notifier.send_text(text)
        except NotifyError as exc:
            log.warning("Could not send a status message: %s", exc)

    async def _pause(self, bounds: tuple[float, float]) -> None:
        await self._sleep(random.uniform(*bounds))
