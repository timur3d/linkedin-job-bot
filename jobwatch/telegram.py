"""
The Telegram side: sending jobs, the buttons under them, and the commands.

The bot is not running between checks. Each run first catches up on what happened in the
chat since the last one (button presses, commands), then checks LinkedIn and posts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from aiogram.utils.token import TokenValidationError

from .config import Config, ConfigError, Settings
from .formatting import applied_message, job_message
from .linkedin import LinkedInSource
from .models import StoredJob
from .monitor import CycleReport, Monitor, NotifyError
from .storage import Storage

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]
HANDLED_UPDATES_KEPT = 200  # ids remembered so a re-delivered update is never applied twice

HELP = (
    "I check LinkedIn on a schedule and post new jobs that fit your search here.\n\n"
    "/status — last check and totals\n"
    "/applied — jobs you marked as applied\n\n"
    "Under each job: <b>Mark applied</b> keeps track of where you sent a CV, "
    "<b>Hide</b> removes a job you don't want.\n\n"
    "I only wake up for each check, so buttons and commands take effect at the next one. "
    "To check sooner, press <b>Run workflow</b> on the repository's Actions page."
)


def job_keyboard(stored: StoredJob) -> InlineKeyboardMarkup:
    job_id = stored.job.id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Open on LinkedIn", url=stored.job.url)],
            [
                InlineKeyboardButton(
                    text="✅ Applied" if stored.applied else "Mark applied",
                    callback_data=f"apply:{job_id}",
                ),
                InlineKeyboardButton(text="Hide", callback_data=f"hide:{job_id}"),
            ],
        ]
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

    async def send_job(self, stored: StoredJob) -> int | None:
        message = await self._send(job_message(stored, self._config), reply_markup=job_keyboard(stored))
        return message.message_id

    async def send_text(self, text: str) -> None:
        await self._send(text)

    async def _send(self, text: str, **kwargs) -> Message:
        chat_id = self.chat_id
        if chat_id is None:
            raise NotifyError("no chat yet: send /start to the bot")
        for retry in (False, True):
            try:
                return await self._bot.send_message(chat_id, text, **kwargs)
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
            log.info("Handled %d button presses and commands from Telegram", handled)

        report = await self.monitor.run_cycle()

        if self.status_requested and self.notifier.ready:
            try:
                await self.notifier.send_text(self.status_text())
            except NotifyError as exc:
                log.warning("Could not send the status reply: %s", exc)
        self.status_requested = False
        return report

    async def register_commands(self) -> None:
        """Fill Telegram's command menu. Once is enough, so later runs skip the call."""
        if self.storage.get_meta("commands") == "set":
            return
        await self.bot.set_my_commands(
            [
                BotCommand(command="status", description="Last check and totals"),
                BotCommand(command="applied", description="Jobs you marked as applied"),
                BotCommand(command="help", description="What this bot does"),
            ]
        )
        self.storage.set_meta("commands", "set")

    async def process_pending_updates(self, dispatcher: Dispatcher) -> int:
        """
        Run everything Telegram queued since the last run through the handlers.

        Telegram keeps undelivered updates for 24 hours and forgets one once a later call
        passes an offset beyond it. If a run dies before that call, the same updates come
        back, so the ids already handled are remembered: "Mark applied" is a toggle and
        must not run twice.
        """
        already_handled = self._handled_update_ids()
        presses_this_run: set[tuple[str, int]] = set()
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
                if update.update_id in already_handled:
                    continue
                if not self._is_repeat_press(update, presses_this_run):
                    try:
                        await dispatcher.feed_update(self.bot, update)
                    except Exception:
                        log.exception("Could not handle Telegram update %s", update.update_id)
                    handled += 1
                already_handled.append(update.update_id)
                self.storage.set_meta("handled_updates", json.dumps(already_handled[-HANDLED_UPDATES_KEPT:]))
        return handled

    def _handled_update_ids(self) -> list[int]:
        try:
            ids = json.loads(self.storage.get_meta("handled_updates", "[]") or "[]")
        except ValueError:
            return []
        return [int(i) for i in ids] if isinstance(ids, list) else []

    @staticmethod
    def _is_repeat_press(update: Update, seen: set[tuple[str, int]]) -> bool:
        """
        The same button tapped again before anything visibly changed.

        Between runs a tap gets no response, so people tap twice. Each tap was made looking
        at the same button, so they all mean the same single action.
        """
        query = update.callback_query
        if query is None or query.data is None or query.message is None:
            return False
        key = (query.data, query.message.message_id)
        if key in seen:
            return True
        seen.add(key)
        return False

    def status_text(self) -> str:
        lines = ["<b>Status</b>"]
        report = self.monitor.last_report
        if report and report.finished:
            lines.append(f"Last check {_clock(report.finished)}: {escape(report.summary())}")
        counts = self.storage.counts()
        lines.append(
            f"Watching {len(self.monitor.queries)} searches. "
            f"So far: {sum(v for k, v in counts.items() if k != 'applied')} postings seen, "
            f"{counts.get('sent', 0) + counts.get('dismissed', 0)} sent, {counts['applied']} applied."
        )
        waiting = counts.get("pending", 0) + counts.get("matched", 0)
        if waiting:
            lines.append(f"{waiting} postings are still being processed.")
        return "\n".join(lines)


def _clock(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


async def _answer(query: CallbackQuery, text: str, *, alert: bool = False) -> None:
    """The little toast after a button press. Telegram refuses it once the press is more than
    a few seconds old, which is the usual case here, so a failure is not an error."""
    try:
        await query.answer(text, show_alert=alert)
    except TelegramAPIError as exc:
        log.debug("Could not answer a button press: %s", exc)


def build_router(app: App) -> Router:
    root = Router()
    owner = Router()  # everything except /start is only for the chat the bot posts to

    # These must be coroutines: aiogram runs plain functions in a worker thread, and the
    # chat id may be read from SQLite, which only allows the thread that opened it.
    async def from_owner_chat(message: Message) -> bool:
        return message.chat.id == app.notifier.chat_id

    async def pressed_in_owner_chat(query: CallbackQuery) -> bool:
        return query.message is not None and query.message.chat.id == app.notifier.chat_id

    owner.message.filter(from_owner_chat)
    owner.callback_query.filter(pressed_in_owner_chat)

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

    @owner.message(Command("applied"))
    async def applied(message: Message) -> None:
        await message.answer(applied_message(app.storage.applied()))

    @owner.callback_query(F.data.startswith("apply:"))
    async def toggle_applied(query: CallbackQuery) -> None:
        job_id = query.data.split(":", 1)[1]
        stored = app.storage.get(job_id)
        if stored is None:
            await _answer(query, "I no longer have this job on record.", alert=True)
            return
        app.storage.set_applied(job_id, not stored.applied)
        stored = app.storage.get(job_id)
        if isinstance(query.message, Message):
            try:
                await query.message.edit_reply_markup(reply_markup=job_keyboard(stored))
            except TelegramAPIError as exc:
                log.debug("Could not update the buttons: %s", exc)
        await _answer(query, "Marked as applied" if stored.applied else "Unmarked")

    @owner.callback_query(F.data.startswith("hide:"))
    async def hide(query: CallbackQuery) -> None:
        job_id = query.data.split(":", 1)[1]
        app.storage.mark_dismissed(job_id)
        if isinstance(query.message, Message):
            try:
                await query.message.delete()
            except TelegramAPIError:
                # Telegram won't delete messages older than 48 hours; collapse it instead.
                stored = app.storage.get(job_id)
                title = escape(stored.job.title) if stored else "Job"
                try:
                    await query.message.edit_text(f"<s>{title}</s> (hidden)", reply_markup=None)
                except TelegramAPIError as exc:
                    log.debug("Could not hide the message: %s", exc)
        await _answer(query, "Hidden")

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
