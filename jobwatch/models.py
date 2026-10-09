"""Plain data types shared across the bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


@dataclass(frozen=True)
class Job:
    """A posting as it appears in LinkedIn search results."""

    id: str  # JobSpy's id, e.g. "li-4012345678"
    title: str
    company: str
    location: str
    url: str
    date_posted: date | None = None
    is_remote: bool = False


@dataclass(frozen=True)
class JobDetails:
    """What the posting's own page adds: the description and LinkedIn's criteria list."""

    description: str = ""
    employment_type: str | None = None  # "fulltime", "parttime", "internship", "contract", ...
    seniority: str | None = None  # "entry level", "internship", "mid-senior level", ...


class Decision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    NEED_DETAILS = "need_details"  # the title is unclear; the description decides


@dataclass(frozen=True)
class TrackMatch:
    """One track accepting a job, and why."""

    track_id: str
    reason: str
    unverified: bool = False  # accepted without being able to read the description


@dataclass(frozen=True)
class Outcome:
    """The verdict on a job across all tracks."""

    decision: Decision
    matches: tuple[TrackMatch, ...] = ()
    flags: tuple[str, ...] = ()
    starred: bool = False
    reason: str = ""  # why it was rejected, or what is still needed

    @property
    def track_ids(self) -> tuple[str, ...]:
        return tuple(match.track_id for match in self.matches)

    @property
    def unverified(self) -> bool:
        return bool(self.matches) and all(match.unverified for match in self.matches)


@dataclass
class StoredJob:
    """A job together with everything the bot has decided about it."""

    job: Job
    status: str  # pending | rejected | matched | sent | duplicate
    title_match: bool = False  # the title alone was enough to accept it
    attempts: int = 0  # failed tries at reading the description
    tracks: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    starred: bool = False
    reason: str = ""
    unverified: bool = False
    employment_type: str | None = None
    seniority: str | None = None
    description: str | None = None  # the posting's text, kept only until the job has been sent


@dataclass
class SearchResult:
    """Jobs from one search, and whether LinkedIn answered it properly."""

    jobs: list[Job] = field(default_factory=list)
    ok: bool = True
    error: str = ""
