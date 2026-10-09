"""LinkedIn access through JobSpy. Everything here blocks, so call it from a worker thread."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

import pandas as pd
from jobspy import scrape_jobs

from .models import Job, JobDetails, SearchResult

log = logging.getLogger(__name__)

# JobSpy reports a blocked or failed request by logging an error and returning whatever
# it has, so an empty result alone can't tell "nothing new" from "LinkedIn said no".
_JOBSPY_LOGGER = "JobSpy:LinkedIn"


class _ErrorCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _jobspy_errors() -> Iterator[list[str]]:
    logger = logging.getLogger(_JOBSPY_LOGGER)
    collector = _ErrorCollector()
    logger.addHandler(collector)
    try:
        yield collector.messages
    finally:
        logger.removeHandler(collector)


class LinkedInSource:
    def __init__(self, proxies: tuple[str, ...] = ()):
        self._proxies = list(proxies) or None
        self._detail_client: Any = None
        self.details_available = True

    def search(self, term: str, location: str, hours_old: int, results_wanted: int) -> SearchResult:
        """Postings from the last `hours_old` hours for one search term in one location."""
        with _jobspy_errors() as errors:
            try:
                frame = scrape_jobs(
                    site_name=["linkedin"],
                    search_term=term,
                    location=location,
                    hours_old=hours_old,
                    results_wanted=results_wanted,
                    proxies=self._proxies,
                    verbose=0,
                )
            except Exception as exc:  # JobSpy raises on malformed pages as well as on network errors
                return SearchResult(ok=False, error=_brief(f"{type(exc).__name__}: {exc}"))
        return SearchResult(jobs=_jobs_from_frame(frame), ok=not errors, error=_brief("; ".join(errors)))

    def fetch_details(self, job_id: str) -> JobDetails | None:
        """
        The description and criteria from the posting's page, or None if it couldn't be read.

        JobSpy's public API only fetches descriptions for a whole search, which would re-read
        every posting on every check. Its LinkedIn scraper has a per-posting method, so this
        calls that directly. It is a private method: requirements.txt pins the JobSpy version,
        and if the method ever changes shape the bot carries on with titles only.
        """
        if not self.details_available:
            return None
        try:
            if self._detail_client is None:
                self._detail_client = self._new_detail_client()
            raw = self._detail_client._fetch_details(job_id.removeprefix("li-"))
        except (AttributeError, TypeError, ImportError) as exc:
            self.details_available = False
            log.warning("This JobSpy version has no per-posting details (%s); continuing with titles only.", exc)
            return None
        except Exception as exc:
            log.debug("Reading %s failed: %s", job_id, exc)
            return None
        if not isinstance(raw, dict):
            return None

        description = _text(raw.get("description")) or ""
        employment_type = _job_type_name(raw.get("job_type"))
        seniority = _text(raw.get("job_level"))
        if not (description or employment_type or seniority):
            return None  # blocked, redirected to a login wall, or the posting is gone
        return JobDetails(description=description, employment_type=employment_type, seniority=seniority)

    def _new_detail_client(self) -> Any:
        from jobspy.linkedin import LinkedIn
        from jobspy.model import DescriptionFormat, ScraperInput

        client = LinkedIn(proxies=self._proxies)
        client.scraper_input = ScraperInput(description_format=DescriptionFormat.PLAIN)
        return client


def _brief(error: str, limit: int = 160) -> str:
    """Network errors run to several lines; the full text is already in JobSpy's own log line."""
    error = " ".join(error.split())
    return error if len(error) <= limit else error[: limit - 1].rstrip() + "…"


def _jobs_from_frame(frame: pd.DataFrame | None) -> list[Job]:
    if frame is None or frame.empty:
        return []
    jobs = []
    for row in frame.to_dict("records"):
        job_id, title, url = _text(row.get("id")), _text(row.get("title")), _text(row.get("job_url"))
        if not (job_id and title and url):
            continue
        remote = row.get("is_remote")
        jobs.append(
            Job(
                id=job_id,
                title=title,
                company=_text(row.get("company")) or "Unknown company",
                location=_text(row.get("location")) or "",
                url=url,
                date_posted=_as_date(row.get("date_posted")),
                is_remote=bool(remote) if not _missing(remote) else False,
            )
        )
    return jobs


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):  # lists and other non-scalars
        return False


def _text(value: Any) -> str | None:
    if _missing(value):
        return None
    return str(value).strip() or None


def _as_date(value: Any) -> date | None:
    if _missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _job_type_name(value: Any) -> str | None:
    """JobSpy gives a list of JobType enums whose value is a tuple of spellings; take the first."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    spellings = getattr(value, "value", value)
    if isinstance(spellings, (list, tuple)):
        spellings = spellings[0] if spellings else None
    return _text(spellings)
