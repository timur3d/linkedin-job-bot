"""Prints what would be sent instead of sending it. Used by the dry run."""

from __future__ import annotations

import re
from html import unescape

from .config import Config
from .formatting import jobs_file
from .models import StoredJob

_TAGS = re.compile(r"<[^>]+>")


class ConsoleNotifier:
    ready = True

    def __init__(self, config: Config):
        self._config = config

    async def send_batch(self, jobs: list[StoredJob]) -> None:
        # The file without its descriptions is the readable summary: every job with its link,
        # tags and the reason it matched.
        _name, text = jobs_file(jobs, self._config, with_descriptions=False)
        print(text)

    async def send_text(self, text: str) -> None:
        print(unescape(_TAGS.sub("", text)))
        print()
