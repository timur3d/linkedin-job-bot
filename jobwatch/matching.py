"""Decides whether a job is relevant. Pure logic: no network, no database."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import cached_property

from .config import Config, ConfigError, Rule, Track
from .models import Decision, Job, JobDetails, Outcome, TrackMatch

_HEBREW_LETTER = "א-ת"
_HEBREW_MARKS = re.compile(r"[֑-ׇ]")
# "מעצב/ת", "סטודנט/ית", "מפתח.ת": drop the gender suffix so one term covers every spelling.
_GENDER_SUFFIX = re.compile(rf"(?<=[{_HEBREW_LETTER}])[/.\\](?:ית|ות|ים|ת|ה)(?![{_HEBREW_LETTER}])")
_SEPARATORS = re.compile(r"[^\w+#&]+|_+")


def normalize(text: str | None) -> str:
    """Lowercase, punctuation to spaces: "UI/UX Designer (Part-Time)" -> "ui ux designer part time"."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _HEBREW_MARKS.sub("", text)
    text = _GENDER_SUFFIX.sub("", text)
    return " ".join(_SEPARATORS.sub(" ", text).split())


class Text:
    """A piece of text with its normalized form computed once."""

    def __init__(self, raw: str | None):
        self.raw = raw or ""

    @cached_property
    def norm(self) -> str:
        return normalize(self.raw)


@dataclass(frozen=True)
class _Term:
    label: str
    pattern: re.Pattern[str]
    on_raw: bool = False


def _compile(term: str, where: str) -> _Term:
    if term.startswith("re:"):
        try:
            return _Term(label=term[3:], pattern=re.compile(term[3:]), on_raw=True)
        except re.error as exc:
            raise ConfigError(f"{where}: bad regular expression {term[3:]!r}: {exc}") from None
    norm = normalize(term)
    if not norm:
        raise ConfigError(f"{where}: the term {term!r} has no letters or digits")
    body = re.escape(norm)
    if norm.isascii():
        # Whole words only, so "intern" doesn't match "internal". Hebrew letters count as a
        # boundary, which lets "ל-UX" match "ux".
        plural = "s?" if norm[-1].isalpha() else ""
        return _Term(label=term, pattern=re.compile(rf"(?<![a-z0-9]){body}{plural}(?![a-z0-9])"))
    # Hebrew attaches prefixes and suffixes to the word, so match anywhere.
    return _Term(label=term, pattern=re.compile(body))


class TermGroup:
    """A named list of terms; finds which of them occur in a text."""

    def __init__(self, name: str, terms: tuple[str, ...]):
        self.name = name
        self._terms = [_compile(term, f"term_groups.{name}") for term in terms]

    def find(self, text: Text, limit: int | None = None) -> list[str]:
        """The distinct terms found, in list order; stops early once `limit` are found."""
        found: list[str] = []
        for term in self._terms:
            match = term.pattern.search(text.raw if term.on_raw else text.norm)
            if not match:
                continue
            # A regex is unreadable in a message, so show what it matched instead.
            label = (match.group(0).strip() or term.label) if term.on_raw else term.label
            if label not in found:
                found.append(label)
                if limit is not None and len(found) >= limit:
                    break
        return found

    def first(self, text: Text) -> str | None:
        found = self.find(text, limit=1)
        return found[0] if found else None


def _key(value: str) -> str:
    """ "Part-time", "part time" and "parttime" all compare equal."""
    return normalize(value).replace(" ", "")


def _employment_keys(details: JobDetails | None) -> set[str]:
    """LinkedIn's "Employment type" and "Seniority level" for the posting."""
    if details is None:
        return set()
    return {_key(value) for value in (details.employment_type, details.seniority) if value}


def _quote(terms: list[str], limit: int = 3) -> str:
    return ", ".join(f"“{term}”" for term in terms[:limit])


@dataclass(frozen=True)
class _TrackResult:
    decision: Decision
    reason: str
    unverified: bool = False


class Matcher:
    def __init__(self, config: Config):
        self.config = config
        self._groups = {name: TermGroup(name, terms) for name, terms in config.term_groups.items()}
        self._locations = TermGroup("exclude_locations", config.exclude_locations)
        self._companies = TermGroup("exclude_companies", config.exclude_companies)

    def evaluate(self, job: Job, details: JobDetails | None = None, *, final: bool = False) -> Outcome:
        """
        Judge a job against every track.

        Without `details`, a job whose title is unclear comes back as NEED_DETAILS. Pass
        `final=True` when the description can't be had: each rule's `without_description`
        then decides.
        """
        title = Text(job.title)
        if hit := self._locations.first(Text(job.location)):
            return Outcome(Decision.REJECT, reason=f"location excluded ({hit})")
        if hit := self._companies.first(Text(job.company)):
            return Outcome(Decision.REJECT, reason=f"company excluded ({hit})")

        body = Text(details.description) if details else None
        matches: list[TrackMatch] = []
        waiting: list[str] = []
        rejected: list[str] = []
        for track in self.config.tracks:
            result = self._evaluate_track(track, title, details, body, final)
            if result.decision is Decision.ACCEPT:
                matches.append(TrackMatch(track.id, result.reason, result.unverified))
            elif result.decision is Decision.NEED_DETAILS:
                waiting.append(f"{track.id}: {result.reason}")
            else:
                rejected.append(f"{track.id}: {result.reason}")

        if matches:
            flags = self._flags(job, title, details, body)
            starred = any(flag in self.config.track(match.track_id).star for match in matches for flag in flags)
            return Outcome(Decision.ACCEPT, tuple(matches), tuple(flags), starred)
        if waiting:
            return Outcome(Decision.NEED_DETAILS, reason="; ".join(waiting))
        return Outcome(Decision.REJECT, reason="; ".join(rejected))

    def _evaluate_track(
        self, track: Track, title: Text, details: JobDetails | None, body: Text | None, final: bool
    ) -> _TrackResult:
        for name in track.exclude_title:
            if hit := self._groups[name].first(title):
                return _TrackResult(Decision.REJECT, f"title has “{hit}” ({name})")

        waiting_on: list[str] = []
        misses: list[str] = []
        for rule in track.rules:
            title_hits, ruled_out = self._title_hits(rule, title)
            if ruled_out:
                misses.append(f"{rule.name}: ruled out by {ruled_out}")
            if not title_hits or ruled_out:
                continue
            why = f"{rule.name}: {_quote(title_hits)}"
            if not rule.needs_details:
                return _TrackResult(Decision.ACCEPT, why)
            if details is None:
                if not final:
                    waiting_on.append(rule.name)
                elif rule.without_description == "accept":
                    return _TrackResult(Decision.ACCEPT, why, unverified=True)
                else:
                    misses.append(f"{rule.name}: description unavailable")
                continue
            confirmed = self._confirm(rule, details, body)
            if confirmed:
                return _TrackResult(Decision.ACCEPT, f"{why}; {confirmed}")
            misses.append(f"{rule.name}: not confirmed by the description")

        if waiting_on:
            return _TrackResult(Decision.NEED_DETAILS, f"needs the description ({'; '.join(waiting_on)})")
        return _TrackResult(Decision.REJECT, "; ".join(misses) or "no rule matched the title")

    def _title_hits(self, rule: Rule, title: Text) -> tuple[list[str], str]:
        """
        (one matched term per required group, what rules the title out).

        The first is empty when the title lacks a required group; the second is set when
        the title has everything required but also a term from a `title_not` group.
        """
        hits = []
        for name in rule.title:
            hit = self._groups[name].first(title)
            if hit is None:
                return [], ""
            hits.append(hit)
        for name in rule.title_not:
            if hit := self._groups[name].first(title):
                return hits, f"“{hit}” ({name})"
        return list(dict.fromkeys(hits)), ""

    def _confirm(self, rule: Rule, details: JobDetails, body: Text | None) -> str:
        """Why the posting's page satisfies the rule, or "" if it doesn't."""
        listed = _employment_keys(details)
        for wanted in rule.or_employment:
            if _key(wanted) in listed:
                return f"LinkedIn lists it as {wanted}"
        if rule.description and body is not None:
            hits = self._groups[rule.description].find(body, limit=max(rule.min_description_hits, 3))
            if len(hits) >= rule.min_description_hits:
                return f"description mentions {_quote(hits)}"
        return ""

    def _flags(self, job: Job, title: Text, details: JobDetails | None, body: Text | None) -> list[str]:
        flags = []
        listed = _employment_keys(details)
        for highlight in self.config.highlights:
            hit = bool(highlight.title and self._groups[highlight.title].first(title))
            hit = hit or any(_key(value) in listed for value in highlight.employment)
            hit = hit or bool(
                highlight.description and body is not None and self._groups[highlight.description].first(body)
            )
            hit = hit or (highlight.name == "remote" and job.is_remote)
            if hit:
                flags.append(highlight.name)
        return flags
