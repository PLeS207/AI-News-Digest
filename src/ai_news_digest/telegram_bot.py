"""Telegram adapter (long polling) + OpenClaw Coordinator bridge. Entry point of the `app` service.

The Coordinator agent itself runs inside OpenClaw (service `openclaw`); this process owns the
Telegram transport, the allowlist, the backend tools (SQLite memory, A2A client) and delivery.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from ai_news_digest.config import (
    ConfigError,
    a2a_max_attempts,
    a2a_timeout_seconds,
    allowed_telegram_user_id,
    editor_db_path,
    load_digest_defaults,
    load_sources,
    openclaw_agent_id,
    openclaw_gateway_token,
    openclaw_timeout_seconds,
    openclaw_url,
    researcher_url,
    telegram_token,
    trace_dir,
)
from ai_news_digest.coordinator.delivery import resend_last_digest
from ai_news_digest.coordinator.memory import MemoryStore
from ai_news_digest.coordinator.openclaw_client import OpenClawClient
from ai_news_digest.coordinator.researcher_client import ResearcherClient
from ai_news_digest.coordinator.service import CoordinatorService
from ai_news_digest.tracing import Tracer, new_id

log = logging.getLogger("app")

HELP_TEXT = (
    "Я собираю AI-новости из RSS и готовлю дайджест с учётом ваших интересов.\n"
    "Coordinator работает в OpenClaw, поиск новостей — отдельный агент Researcher (A2A).\n\n"
    "Примеры:\n"
    "• Запомни: меня интересуют AI agents, MCP и AWS Bedrock. Пиши по-русски, максимум 3 новости\n"
    "• /digest или «Подготовь дайджест AI-новостей за последние 72 часа»\n"
    "• Какие интересы и настройки ты сохранил?\n"
    "• /last или «Покажи последний дайджест»\n"
    "• Собери ещё один дайджест, пропусти уже отправленные новости\n\n"
    "Команды: /digest — новый дайджест, /settings — настройки из памяти, /last — последний дайджест, "
    "/new — новая беседа (память сохраняется), /help"
)

HEARTBEAT_FILE = Path("/tmp/app-heartbeat")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class Gatekeeper:
    """Checks the Telegram user id BEFORE anything reaches OpenClaw, the model or memory."""

    def __init__(self, allowed_user_id: int, tracer: Tracer):
        self.allowed_user_id = allowed_user_id
        self.tracer = tracer

    def allowed(self, user_id: int | None) -> bool:
        ok = user_id is not None and user_id == self.allowed_user_id
        if not ok:
            # The rejected id is deliberately not logged.
            self.tracer.event(new_id("tr_"), agent="app", kind="auth", name="reject_user",
                              status="denied", error="user is not in ALLOWED_TELEGRAM_USER_ID")
        return ok


async def process_text(
    *, gate: Gatekeeper, coordinator: CoordinatorService, user_id: int | None, chat_key: str, text: str,
    send: Callable[[str], Awaitable[Any]], on_start: Callable[[], Awaitable[Any]] | None = None,
    command: str | None = None,
) -> dict[str, Any]:
    """Transport-independent message flow (used by the Telegram handlers and tests)."""
    if not gate.allowed(user_id):
        await send("⛔ Доступ запрещён. Этот бот обслуживает только своего владельца.")
        return {"rejected": True}
    if on_start:
        await on_start()
    turn = await coordinator.handle(text, chat_key, command=command)
    report = await coordinator.deliver(turn, send)
    return {"rejected": False, "trace_id": turn.trace_id, "kind": turn.kind, "failed": turn.failed,
            "openclaw_run_ids": turn.openclaw_run_ids, **report}


def settings_text(memory: MemoryStore) -> str:
    settings, stored = memory.get_settings()
    ru = settings.language.lower().startswith("ru")
    src = "SQLite" if stored else ("значения по умолчанию" if ru else "defaults")
    delivered = len(memory.delivered_urls(limit=10_000))
    if ru:
        lines = [f"⚙️ Настройки (источник: {src})", f"Интересы: {', '.join(settings.interests)}",
                 f"Язык: {settings.language}", f"Новостей в дайджесте: до {settings.max_items}",
                 f"Период: {settings.period_hours} ч"]
        if settings.notes:
            lines.append("Заметки: " + "; ".join(settings.notes))
        lines.append(f"Доставлено ссылок: {delivered}")
    else:
        lines = [f"⚙️ Settings (source: {src})", f"Interests: {', '.join(settings.interests)}",
                 f"Language: {settings.language}", f"Max items: {settings.max_items}",
                 f"Period: {settings.period_hours} h"]
        if settings.notes:
            lines.append("Notes: " + "; ".join(settings.notes))
        lines.append(f"Delivered links: {delivered}")
    return "\n".join(lines)


def build_application(token: str, gate: Gatekeeper, coordinator: CoordinatorService, memory: MemoryStore,
                      request: Any = None) -> Application:
    def sender(update: Update) -> Callable[[str], Awaitable[Any]]:
        async def send(text: str) -> Any:
            return await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML,
                                                            link_preview_options=NO_PREVIEW)
        return send

    async def guarded(update: Update) -> bool:
        user = update.effective_user
        if not gate.allowed(user.id if user else None):
            if update.effective_chat:
                await update.effective_chat.send_message("⛔ Доступ запрещён.")
            return False
        return True

    async def run_turn(update: Update, text: str, command: str | None = None) -> None:
        chat = update.effective_chat
        user = update.effective_user

        async def typing() -> None:
            await chat.send_action(ChatAction.TYPING)

        try:
            await process_text(gate=gate, coordinator=coordinator, user_id=user.id if user else None,
                               chat_key=str(chat.id), text=text, send=sender(update), on_start=typing,
                               command=command)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to process message")
            await chat.send_message(f"❌ Внутренняя ошибка: {type(exc).__name__}. Подробности в dashboard.")

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guarded(update):
            await update.effective_chat.send_message(HELP_TEXT)

    async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guarded(update):
            coordinator.new_conversation(str(update.effective_chat.id))
            await update.effective_chat.send_message(
                "🆕 Новая беседа: создана новая сессия OpenClaw Coordinator, краткосрочный контекст пуст. "
                "Настройки, история дайджестов и доставленные ссылки сохранены в SQLite.")

    async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guarded(update):
            coordinator.tracer.event(new_id("tr_"), agent="app", kind="memory_read", name="settings(/settings)",
                                     status="completed")
            await update.effective_chat.send_message(html.escape(settings_text(memory), quote=False),
                                                     parse_mode=ParseMode.HTML)

    async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guarded(update):
            trace_id = new_id("tr_")
            with coordinator.tracer.span(trace_id, agent="app", kind="run", name="/last"):
                await resend_last_digest(send=sender(update), memory=memory, tracer=coordinator.tracer,
                                         trace_id=trace_id)

    async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guarded(update):
            await run_turn(update, update.effective_message.text or "/digest", command="digest")

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message is None or not update.effective_message.text:
            return
        await run_turn(update, update.effective_message.text)

    async def heartbeat(app: Application) -> None:
        async def beat() -> None:
            while True:
                HEARTBEAT_FILE.write_text(str(time.time()))
                await asyncio.sleep(20)
        app.create_task(beat())

    builder = Application.builder().token(token).post_init(heartbeat)
    if request is not None:  # tests inject a fake Bot API transport
        builder = builder.request(request).get_updates_request(request)
    application = builder.build()
    application.add_handler(CommandHandler(["start", "help"], cmd_help))
    application.add_handler(CommandHandler("new", cmd_new))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("last", cmd_last))
    application.add_handler(CommandHandler("digest", cmd_digest))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application


def build_coordinator(tracer: Tracer, memory: MemoryStore) -> CoordinatorService:
    researcher = ResearcherClient(researcher_url(), tracer, timeout_seconds=a2a_timeout_seconds(),
                                  max_attempts=a2a_max_attempts())
    openclaw = OpenClawClient(openclaw_url(), openclaw_gateway_token(), agent_id=openclaw_agent_id(),
                              timeout_seconds=openclaw_timeout_seconds())
    return CoordinatorService(openclaw=openclaw, memory=memory, tracer=tracer, research=researcher.research,
                              sources=load_sources())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs that contain the bot token
    try:
        token = telegram_token()
        allowed = allowed_telegram_user_id()
        tracer = Tracer("app", trace_dir())
        memory = MemoryStore(editor_db_path(), load_digest_defaults())
        coordinator = build_coordinator(tracer, memory)
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    gate = Gatekeeper(allowed, tracer)
    log.info("app started: coordinator=%s @ %s, researcher=%s, db=%s memory=%s", coordinator.openclaw.model,
             openclaw_url(), researcher_url(), editor_db_path(), memory.stats())
    application = build_application(token, gate, coordinator, memory)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
