"""
What the bot posts after a check that found jobs: a short list for the chat, and a text
file with every job in full. No Telegram types here, so it is easy to test.
"""

from __future__ import annotations

from datetime import datetime
from html import escape

from .config import Config, Track
from .models import StoredJob

_LIST_BUDGET = 3500  # visible characters; Telegram rejects messages over 4096
_LIST_MAX_JOBS = 80  # Telegram formats at most 100 links and bold spans per message
_RULE = "=" * 78
_EMPLOYMENT_LABELS = {
    "fulltime": "Full-time",
    "parttime": "Part-time",
    "internship": "Internship",
    "contract": "Contract",
    "temporary": "Temporary",
}


def list_message(jobs: list[StoredJob], config: Config) -> str:
    """The chat message, as HTML: jobs grouped by track, each title a link to the posting."""
    header = _count(len(jobs))
    lines = [f"<b>{header}</b>"]
    room = _LIST_BUDGET - len(header)
    shown = 0
    for track, group in _by_track(jobs, config):
        heading = f"{track.emoji} {track.label}" if track else "💼 Other"
        entries: list[str] = []
        for stored in group:
            plain, html = _list_entry(stored, track)
            cost = len(plain) + 1 + (0 if entries else len(heading) + 2)
            if cost > room or shown + len(entries) >= _LIST_MAX_JOBS:
                break
            room -= cost
            entries.append(html)
        if entries:
            lines += ["", f"<b>{escape(heading)}</b>", *entries]
            shown += len(entries)
        if len(entries) < len(group):
            break  # out of room: the rest is only in the file
    lines.append("")
    if shown < len(jobs):
        lines.append(f"…and {len(jobs) - shown} more. All {len(jobs)} are in the file, with full descriptions.")
    else:
        lines.append("Full descriptions are in the file.")
    return "\n".join(lines)


def _list_entry(stored: StoredJob, track: Track | None) -> tuple[str, str]:
    """One job as a line of the list: (what the reader sees, the HTML for it)."""
    job = stored.job
    details = " · ".join(part for part in (job.company, _city(job.location), ", ".join(_tags(stored, track))) if part)
    mark = "⭐" if stored.starred else "•"
    link = f'<a href="{escape(job.url, quote=True)}">{escape(job.title)}</a>'
    return f"{mark} {job.title} · {details}", f"{mark} {link} · {escape(details)}"


def jobs_file(
    jobs: list[StoredJob], config: Config, *, now: datetime | None = None, with_descriptions: bool = True
) -> tuple[str, str]:
    """(file name, text) of the file that goes with the list: every job, with its description."""
    now = (now or datetime.now()).astimezone()
    lines = ["LinkedIn job bot", f"{_count(len(jobs))}, found by the check of {now:%Y-%m-%d %H:%M %Z}".rstrip()]
    number = 0
    for _track, group in _by_track(jobs, config):
        for stored in group:
            number += 1
            lines += ["", *_job_lines(number, stored, config, with_descriptions)]
    return f"jobs-{now:%Y-%m-%d-%H%M}.txt", "\n".join(lines) + "\n"


def _job_lines(number: int, stored: StoredJob, config: Config, with_description: bool) -> list[str]:
    job = stored.job
    tracks = [t.label for t in map(config.track, stored.tracks) if t]
    facts = [
        _EMPLOYMENT_LABELS.get(stored.employment_type or "", (stored.employment_type or "").capitalize()),
        (stored.seniority or "").capitalize() if (stored.seniority or "").lower() != "not applicable" else "",
    ]
    fields = [
        ("Link", job.url),
        ("Location", job.location),
        ("Posted", job.date_posted.isoformat() if job.date_posted else ""),
        ("Track", " + ".join(tracks)),
        ("Tags", ", ".join(stored.flags) + (" (starred)" if stored.starred else "")),
        ("Employment", " / ".join(dict.fromkeys(fact for fact in facts if fact))),
        ("Matched on", stored.reason),
    ]
    lines = [_RULE, f"{number}. {_flat(job.title)} | {_flat(job.company)}", _RULE]
    lines += [f"{name + ':':<12}{_flat(value)}" for name, value in fields if value.strip()]
    if with_description:
        lines += ["", "Description:", _description(stored)]
    return lines


def _description(stored: StoredJob) -> str:
    if stored.description and stored.description.strip():
        return stored.description.strip()
    if stored.unverified:
        return "(could not be read; this job was matched on its title alone, so check that it fits)"
    return "(could not be read; open the link for the full posting)"


def _by_track(jobs: list[StoredJob], config: Config) -> list[tuple[Track | None, list[StoredJob]]]:
    """Jobs under the first track that accepted them, in config order; starred first, then newest."""
    groups: dict[str, list[StoredJob]] = {}
    for stored in jobs:
        groups.setdefault(stored.tracks[0] if stored.tracks else "", []).append(stored)
    order = [track.id for track in config.tracks]
    ordered = sorted(groups, key=lambda track_id: order.index(track_id) if track_id in order else len(order))
    return [
        (
            config.track(track_id),
            sorted(
                groups[track_id],
                key=lambda s: (
                    not s.starred,
                    -(s.job.date_posted.toordinal() if s.job.date_posted else 0),
                    s.job.title,
                ),
            ),
        )
        for track_id in ordered
    ]


def _tags(stored: StoredJob, track: Track | None) -> list[str]:
    """Tags worth showing next to a job; "student" under the "CS student" heading says nothing."""
    label_words = set(track.label.lower().split()) if track else set()
    return [flag for flag in stored.flags if flag.lower() not in label_words]


def _city(location: str) -> str:
    """ "Tel Aviv-Yafo, Tel Aviv District, Israel" -> "Tel Aviv-Yafo": the list has no room for more."""
    return location.split(",")[0].strip()


def _count(number: int) -> str:
    return "1 new job" if number == 1 else f"{number} new jobs"


def _flat(text: str) -> str:
    """One line: titles and reasons occasionally carry line breaks."""
    return " ".join(str(text).split())
