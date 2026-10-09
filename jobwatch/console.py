"""Prints matches instead of sending them. Used by --dry-run."""

from __future__ import annotations

import re
from html import unescape

from .config import Config
from .formatting import job_message
from .models import StoredJob

_TAGS = re.compile(r"<[^>]+>")


class ConsoleNotifier:
    ready = True

    def __init__(self, config: Config):
        self._config = config

    async def send_job(self, stored: StoredJob) -> int | None:
        print(unescape(_TAGS.sub("", job_message(stored, self._config))))
        print(stored.job.url)
        print()
        return None

    async def send_text(self, text: str) -> None:
        print(unescape(_TAGS.sub("", text)))
        print()
