"""Telegram adapter (long polling) + Editor wiring. Entry point of the `app` service."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from ai_news_digest.agents.llm import AnthropicBackend
from ai_news_digest.config import (
    ConfigError,
    a2a_max_attempts,
    a2a_timeout_seconds,
    allowed_telegram_user_id,
    editor_db_path,
    load_digest_defaults,
    load_limits,
    load_llm_config,
    load_sources,
    require_api_key,
    researcher_url,
    telegram_token,
    trace_dir,
)
from ai_news_digest.editor.agent import EditorService, settings_text
from ai_news_digest.editor.memory import MemoryStore
from ai_news_digest.editor.researcher_client import ResearcherClient
from ai_news_digest.tracing import Tracer, new_id

log = logging.getLogger("app")

HELP_TEXT = (
    "Я собираю AI-новости из RSS и готовлю дайджест с учётом ваших интересов.\n\n"
    "Примеры:\n"
    "• Запомни: меня интересуют AI agents, MCP и AWS Bedrock. Пиши по-русски, максимум 3 новости\n"
    "• Подготовь дайджест AI-новостей за последние 72 часа\n"
    "• Какие интересы и настройки ты сохранил?\n"
    "• Покажи последний дайджест\n"
    "• Собери ещё один дайджест, пропусти уже отправленные новости\n\n"
    "Команды: /settings — настройки из памяти, /last — последний дайджест, "
    "/new — новая беседа (память сохраняется), /help"
)

HEARTBEAT_FILE = Path("/tmp/app-heartbeat")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class Gatekeeper:
    """Checks the Telegram user id BEFORE anything reaches the model or memory."""

    def __init__(self, allowed_user_id: int, tracer: Tracer):
        self.allowed_user_id = allowed_user_id
        self.tracer = tracer

    def allowed(self, user_id: int | None) -> bool:
        ok = user_id is not None and user_id == self.allowed_user_id
        if not ok:
            # The rejected id is deliberately not logged.
            self.tracer.event(new_id("tr_"), agent="telegram", kind="auth", name="reject_user",
                              status="denied", error="user is not in ALLOWED_TELEGRAM_USER_ID")
        return ok


async def process_text(
    *, gate: Gatekeeper, editor: EditorService, user_id: int | None, chat_key: str, text: str,
    send: Callable[[str], Awaitable[Any]], on_start: Callable[[], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Transport-independent message flow (used by the Telegram handler and tests)."""
    if not gate.allowed(user_id):
        await send("⛔ Доступ запрещён. Этот бот обслуживает только своего владельца.")
        return {"rejected": True}
    if on_start:
        await on_start()
    turn = await editor.handle(text, chat_key)
    report = await editor.deliver(turn, send)
    return {"rejected": False, "trace_id": turn.trace_id, **report}


def build_application(token: str, gate: Gatekeeper, editor: EditorService, memory: MemoryStore,
                      request: Any = None) -> Application:
    async def send_factory(update: Update) -> Callable[[str], Awaitable[Any]]:
        async def send(text: str) -> Any:
            return await update.effective_chat.send_message(
                text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
        return send

    async def guarded(update: Update) -> bool:
        user = update.effective_user
        if not gate.allowed(user.id if user else None):
            if update.effective_chat:
                await update.effective_chat.send_message("⛔ Доступ запрещён.")
            return False
        return True

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guarded(update):
            await update.effective_chat.send_message(HELP_TEXT)

    async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guarded(update):
            cleared = editor.new_conversation(str(update.effective_chat.id))
            await update.effective_chat.send_message(
                f"🆕 Новая беседа: краткосрочный контекст очищен ({cleared} реплик). "
                "Настройки, история дайджестов и доставленные ссылки сохранены в SQLite.")

    async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guarded(update):
            tracer_id = new_id("tr_")
            editor.tracer.event(tracer_id, agent="editor", kind="memory_read", name="settings(/settings)",
                                status="completed")
            await update.effective_chat.send_message(settings_text(memory))

    async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await guarded(update):
            rows = memory.last_digests(1)
            if not rows:
                await update.effective_chat.send_message("Дайджестов пока нет.")
                return
            await update.effective_chat.send_message(rows[0]["rendered_text"][:4096], parse_mode=ParseMode.HTML,
                                                     link_preview_options=NO_PREVIEW)

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message is None or not update.effective_message.text:
            return
        user = update.effective_user
        chat = update.effective_chat

        async def typing() -> None:
            await chat.send_action(ChatAction.TYPING)

        try:
            await process_text(gate=gate, editor=editor, user_id=user.id if user else None,
                               chat_key=str(chat.id), text=update.effective_message.text,
                               send=await send_factory(update), on_start=typing)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to process message")
            await chat.send_message(f"❌ Внутренняя ошибка: {type(exc).__name__}. Подробности в dashboard.")

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
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs that contain the bot token
    try:
        token = telegram_token()
        allowed = allowed_telegram_user_id()
        llm_cfg = load_llm_config()
        require_api_key(llm_cfg)
        sources = load_sources()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    tracer = Tracer("editor", trace_dir())
    memory = MemoryStore(editor_db_path(), load_digest_defaults())
    client = ResearcherClient(researcher_url(), tracer, timeout_seconds=a2a_timeout_seconds(),
                              max_attempts=a2a_max_attempts())
    editor = EditorService(llm=AnthropicBackend(llm_cfg), memory=memory, tracer=tracer,
                           research=client.research, sources=sources, limits=load_limits())
    gate = Gatekeeper(allowed, tracer)
    log.info("Editor started: model=%s researcher=%s db=%s memory=%s", llm_cfg.model, researcher_url(),
             editor_db_path(), memory.stats())
    application = build_application(token, gate, editor, memory)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
