"""Turns jobs and reports into message text. No Telegram types here, so it is easy to test."""

from __future__ import annotations

from datetime import date
from html import escape

from .config import Config
from .models import StoredJob

_MESSAGE_BUDGET = 3800  # Telegram rejects messages over 4096 characters

_EMPLOYMENT_LABELS = {
    "fulltime": "Full-time",
    "parttime": "Part-time",
    "internship": "Internship",
    "contract": "Contract",
    "temporary": "Temporary",
    "volunteer": "Volunteer",
    "other": "Other",
}


def job_message(stored: StoredJob, config: Config, today: date | None = None) -> str:
    """The Telegram message for one job, as HTML."""
    job = stored.job
    tracks = [track for track in map(config.track, stored.tracks) if track]
    heading = " + ".join(f"{track.emoji} {escape(track.label)}" for track in tracks) or "💼 Job"
    # "🎓 CS student · student" says the same thing twice.
    label_words = {word for track in tracks for word in track.label.lower().split()}
    tags = " · ".join(escape(flag) for flag in stored.flags if flag.lower() not in label_words)
    if stored.starred:
        heading += f"  ⭐ {tags}" if tags else "  ⭐"
    elif tags:
        heading += f"  · {tags}"

    lines = [heading, f"<b>{escape(job.title)}</b>"]
    place = " · ".join(escape(part) for part in (job.company, job.location) if part)
    if place:
        lines.append(place)

    facts = []
    if stored.employment_type:
        facts.append(_EMPLOYMENT_LABELS.get(stored.employment_type, stored.employment_type.capitalize()))
    if stored.seniority and stored.seniority.lower() != "not applicable":
        facts.append(stored.seniority.capitalize())
    if posted := _posted(job.date_posted, today or date.today()):
        facts.append(posted)
    facts = list(dict.fromkeys(facts))  # LinkedIn lists internships as both type and level
    if facts:
        lines.append(escape(" · ".join(facts)))

    if stored.unverified:
        lines.append("⚠️ <i>Couldn't read the description, so this one is a guess from the title.</i>")
    if config.show_match_reason and stored.reason:
        lines.append(f"<i>{escape(stored.reason)}</i>")
    return "\n".join(lines)


def _posted(posted: date | None, today: date) -> str:
    if posted is None:
        return ""
    days = (today - posted).days
    if days <= 0:
        return "posted today"
    if days == 1:
        return "posted yesterday"
    return f"posted {posted.strftime('%d %b')}"


def applied_message(jobs: list[StoredJob]) -> str:
    if not jobs:
        return "You haven't marked any job as applied yet. Tap “Mark applied” under a job after you apply."
    lines = [f"<b>Applied ({len(jobs)})</b>"]
    length = len(lines[0])
    for shown, stored in enumerate(jobs):
        job = stored.job
        line = f'• <a href="{escape(job.url, quote=True)}">{escape(job.title)}</a> — {escape(job.company)}'
        if length + len(line) > _MESSAGE_BUDGET:
            lines.append(f"…and {len(jobs) - shown} more")
            break
        lines.append(line)
        length += len(line) + 1
    return "\n".join(lines)
