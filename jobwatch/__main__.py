"""Command line entry point, as .github/workflows/jobwatch.yml calls it."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import telegram
from .config import Config, ConfigError, Settings, load_config
from .console import ConsoleNotifier
from .linkedin import LinkedInSource
from .monitor import CycleReport, Monitor
from .storage import Storage

CONFIG_FILE = "config.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m jobwatch",
        description="Checks LinkedIn for new jobs and posts the relevant ones to Telegram.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--once",
        action="store_true",
        help="a normal run: handle button presses and commands waiting in Telegram, check LinkedIn, send the matches",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="check LinkedIn and print the matches; nothing is sent to Telegram or saved",
    )
    parser.add_argument(
        "--show-rejected", action="store_true", help="with --dry-run: also list what was filtered out, and why"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    try:
        config = load_config(CONFIG_FILE)
        settings = Settings.from_env()
        if args.dry_run:
            return asyncio.run(_dry_run(config, settings, args.show_rejected))
        _report_to_github(asyncio.run(telegram.run(config, settings)))
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        if _on_github():
            print(f"::error title=Configuration::{_one_line(str(exc))}")
        return 2
    return 0


async def _dry_run(config: Config, settings: Settings, show_rejected: bool) -> int:
    storage = Storage(":memory:")  # a dry run never touches the real history
    monitor = Monitor(config, storage, LinkedInSource(settings.proxies), ConsoleNotifier(config))
    print(f"Dry run: {len(monitor.queries)} searches, last {config.first_run_hours} hours. This takes a few minutes.\n")
    report = await monitor.run_cycle()
    rejected = storage.with_status("rejected") if show_rejected else []
    if rejected:
        print("Filtered out:")
        for stored in rejected:
            print(f"  - {stored.job.title} ({stored.job.company}): {stored.reason}")
        print()
    print(report.summary())
    _report_to_github(report)
    return 1 if report.blocked else 0


def _on_github() -> bool:
    return os.getenv("GITHUB_ACTIONS") == "true"


def _one_line(text: str) -> str:
    """Escape a message for a GitHub workflow command."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _report_to_github(report: CycleReport) -> None:
    """Show the result on the run's page instead of only in the log."""
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(f"**Job check:** {report.summary()}\n")
    if not _on_github():
        return
    if report.blocked:
        print(f"::warning title=LinkedIn did not answer::{_one_line(report.summary())}")
    if report.queued:
        print(
            f"::notice title=Jobs waiting::{report.queued} jobs are waiting to be sent. "
            "If you haven't yet, open the bot in Telegram and send /start."
        )


if __name__ == "__main__":
    sys.exit(main())
