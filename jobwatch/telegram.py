"""
The Telegram side: posting each check's jobs, and the few commands.

The bot is not running between checks. Each run first catches up on commands sent since
the last one, then checks LinkedIn and posts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, BufferedInputFile, Message
from aiogram.utils.token import TokenValidationError

from .config import Config, ConfigError, Settings
from .formatting import jobs_file, list_message
from .linkedin import LinkedInSource
from .models import StoredJob
from .monitor import CycleReport, Monitor, NotifyError
from .storage import Storage

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message"]
COMMAND_MENU_VERSION = "3"  # raise when the command list changes, so the menu is set again

HELP = (
    "I check LinkedIn on a schedule. When a check finds new jobs that fit your search, I post a short "
    "list with links, and a text file with the full description of each one.\n\n"
    "/status — last check and totals\n\n"
    "I only wake up for each check, so a command is answered at the next one. "
    "To check sooner, press <b>Run workflow</b> on the repository's Actions page."
)


class TelegramNotifier:
    def __init__(self, bot: Bot, config: Config, storage: Storage, fixed_chat_id: int | None):
        self._bot = bot
        self._config = config
        self._storage = storage
        self._fixed_chat_id = fixed_chat_id

    @property
    def chat_id(self) -> int | None:
        """The CHAT_ID secret, or else the chat that sent the first /start."""
        if self._fixed_chat_id is not None:
            return self._fixed_chat_id
        saved = self._storage.get_meta("chat_id")
        return int(saved) if saved else None

    @property
    def ready(self) -> bool:
        return self.chat_id is not None

    async def send_batch(self, jobs: list[StoredJob]) -> None:
        """One check's jobs: the list as a message, then the file with the descriptions."""
        filename, text = jobs_file(jobs, self._config)
        await self._send(list_message(jobs, self._config))
        count = "this job" if len(jobs) == 1 else f"these {len(jobs)} jobs"
        await self._send(
            f"Full details of {count}.", document=BufferedInputFile(text.encode("utf-8"), filename=filename)
        )

    async def send_text(self, text: str) -> None:
        await self._send(text)

    async def _send(self, text: str, *, document: BufferedInputFile | None = None) -> Message:
        chat_id = self.chat_id
        if chat_id is None:
            raise NotifyError("no chat yet: send /start to the bot")
        for retry in (False, True):
            try:
                if document is not None:
                    return await self._bot.send_document(chat_id, document, caption=text, parse_mode=None)
                return await self._bot.send_message(chat_id, text)
            except TelegramRetryAfter as exc:
                if retry:
                    raise NotifyError(f"Telegram rate limit: {exc}") from exc
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramAPIError as exc:
                raise NotifyError(f"{type(exc).__name__}: {exc}") from exc
        raise AssertionError("unreachable")


class App:
    """One run: catch up on Telegram, check LinkedIn, post."""

    def __init__(self, config: Config, storage: Storage, monitor: Monitor, notifier: TelegramNotifier, bot: Bot):
        self.config = config
        self.storage = storage
        self.monitor = monitor
        self.notifier = notifier
        self.bot = bot
        self.status_requested = False  # /status is answered after the check, with its result

    async def run_once(self, dispatcher: Dispatcher) -> CycleReport:
        try:
            await self.register_commands()
        except TelegramUnauthorizedError:
            raise  # a wrong token: stop, rather than "succeed" without ever delivering
        except TelegramAPIError as exc:
            log.warning("Could not set the command menu: %s", exc)
        handled = await self.process_pending_updates(dispatcher)
        if handled:
            log.info("Handled %d messages from Telegram", handled)

        report = await self.monitor.run_cycle()

        if self.status_requested and self.notifier.ready:
            try:
                await self.notifier.send_text(self.status_text())
            except NotifyError as exc:
                log.warning("Could not send the status reply: %s", exc)
        self.status_requested = False
        return report

    async def register_commands(self) -> None:
        """Fill Telegram's command menu. Once per version of the list is enough, so later runs skip the call."""
        if self.storage.get_meta("commands") == COMMAND_MENU_VERSION:
            return
        await self.bot.set_my_commands(
            [
                BotCommand(command="status", description="Last check and totals"),
                BotCommand(command="help", description="What this bot does"),
            ]
        )
        self.storage.set_meta("commands", COMMAND_MENU_VERSION)

    async def process_pending_updates(self, dispatcher: Dispatcher) -> int:
        """
        Run the messages Telegram queued since the last run through the handlers.

        Telegram keeps undelivered updates for 24 hours and forgets one once a later call
        passes an offset beyond it, which is how each batch is confirmed.
        """
        offset: int | None = None
        handled = 0
        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=offset, limit=100, timeout=0, allowed_updates=ALLOWED_UPDATES
                )
            except TelegramUnauthorizedError:
                raise
            except TelegramAPIError as exc:
                log.warning("Could not read Telegram updates: %s", exc)
                break
            if not updates:
                break
            for update in updates:
                offset = update.update_id + 1
                try:
                    await dispatcher.feed_update(self.bot, update)
                except Exception:
                    log.exception("Could not handle Telegram update %s", update.update_id)
                handled += 1
        return handled

    def status_text(self) -> str:
        lines = ["<b>Status</b>"]
        report = self.monitor.last_report
        if report and report.finished:
            lines.append(f"Last check {_clock(report.finished)}: {escape(report.summary())}")
        counts = self.storage.counts()
        lines.append(
            f"Watching {len(self.monitor.queries)} searches. "
            f"So far: {sum(counts.values())} postings seen, {counts.get('sent', 0)} sent."
        )
        waiting = counts.get("pending", 0) + counts.get("matched", 0)
        if waiting:
            lines.append(f"{waiting} postings are still being processed.")
        return "\n".join(lines)


def _clock(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def build_router(app: App) -> Router:
    root = Router()
    owner = Router()  # everything except /start is only for the chat the bot posts to

    # This must be a coroutine: aiogram runs plain functions in a worker thread, and the
    # chat id may be read from SQLite, which only allows the thread that opened it.
    async def from_owner_chat(message: Message) -> bool:
        return message.chat.id == app.notifier.chat_id

    owner.message.filter(from_owner_chat)

    @root.message(CommandStart())
    async def start(message: Message) -> None:
        if app.notifier.chat_id is None:
            app.storage.set_meta("chat_id", str(message.chat.id))
            log.info("Chat %s claimed the bot", message.chat.id)
            # The claim lives in the saved history; a secret survives that being reset.
            # Jobs found before this point are sent by the check that follows.
            await message.answer(
                "You're set: new jobs will show up in this chat.\n\n" + HELP + "\n\n"
                f"This chat's ID is <code>{message.chat.id}</code>. Add it to the repository as a secret "
                "named CHAT_ID so I keep posting here even if my history is ever reset."
            )
        elif message.chat.id == app.notifier.chat_id:
            await message.answer(HELP)
        else:
            await message.answer("This bot is already set up for another chat.")

    @owner.message(Command("help"))
    async def help_(message: Message) -> None:
        await message.answer(HELP)

    @owner.message(Command("status"))
    async def status(message: Message) -> None:
        app.status_requested = True

    root.include_router(owner)
    return root


_TOKEN_HINT = "Copy the token again from @BotFather: it looks like 123456789:AAE..."


async def run(config: Config, settings: Settings) -> CycleReport:
    """One run, start to finish. Returns what the check found."""
    if not settings.bot_token:
        raise ConfigError(
            "BOT_TOKEN is not set. Create a bot with @BotFather and add its token to the repository "
            "as a secret named BOT_TOKEN"
        )
    try:
        bot = Bot(
            settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
    except TokenValidationError:
        raise ConfigError(f"BOT_TOKEN doesn't look like a Telegram bot token. {_TOKEN_HINT}") from None

    storage = Storage(settings.db_path)
    notifier = TelegramNotifier(bot, config, storage, settings.chat_id)
    monitor = Monitor(config, storage, LinkedInSource(settings.proxies), notifier)
    app = App(config, storage, monitor, notifier, bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(app))
    try:
        return await app.run_once(dispatcher)
    except TelegramUnauthorizedError:
        raise ConfigError(f"Telegram rejected BOT_TOKEN. {_TOKEN_HINT}") from None
    finally:
        await bot.session.close()
        storage.close()
