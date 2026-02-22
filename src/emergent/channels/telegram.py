"""Telegram gateway using aiogram v3."""

from __future__ import annotations

import asyncio
import html
import re
import time
import uuid
from dataclasses import dataclass

import structlog
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from emergent.agent.context import ContextBuilder
from emergent.agent.runtime import AgentRuntime
from emergent.config import EmergentSettings
from emergent.llm.factory import create_llm_client
from emergent.memory.store import MemoryStore
from emergent.memory.summarizer import summarize_conversation
from emergent.observability.banner import ConsoleNotifier

logger = structlog.get_logger(__name__)

MAX_MESSAGE_LENGTH = 4096  # Telegram limit

_STATUS_HISTORY_ACTIVATED = "⚡ Flux listo..."
_STATUS_ANIMATION_FRAMES = (
    "🛣️ Alcanzando 88 millas por hora...",
    "⚡ Generando 1.21 giggawats de potencia...",
    "🕰️ Circuitos temporales...",
    "🚪 Abriendo portal de contexto...",
    "📡 Sincronizando linea temporal...",
    "🧪 Estabilizando flujo temporal...",
    "🗺️ Verificando coordenadas de 1985...",
    "🔧 Calibrando condensador de flujo...",
    "📓 Consultando almanaque deportivo...",
)
_STATUS_ANIMATION_INTERVAL_SECONDS = 2.8
_STATUS_CONFIRM = "⚖️ Linea temporal sensible. OK?"
_STATUS_READY = "✅ 88 mph alcanzadas"
_STATUS_ERROR = "⚠️ Paradoja temporal. Reintento"

# Pending confirmations: {callback_key: asyncio.Event}
_pending_confirmations: dict[str, asyncio.Event] = {}
_confirmation_results: dict[str, bool] = {}


@dataclass
class _StatusHandle:
    chat_id: int
    message_id: int | None
    last_text: str
    paused: bool = False


def _split_message(text: str, max_len: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a long message into chunks."""
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        # Try to split at a newline
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        parts.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return parts


_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(.+)$")
_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")
_CODE_PATTERN = re.compile(r"`([^`]+)`")


def _format_chunk_for_telegram(chunk: str) -> str:
    """Convert a markdown-like chunk to Telegram HTML subset."""
    lines: list[str] = []
    for raw_line in chunk.splitlines():
        line = raw_line.rstrip()
        heading = _HEADING_PATTERN.match(line)
        if heading:
            title = heading.group(1).strip()
            if title.startswith("**") and title.endswith("**") and len(title) >= 4:
                title = title[2:-2].strip()
            line = f"**{title}**"

        escaped = html.escape(line, quote=False)
        escaped = _CODE_PATTERN.sub(r"<code>\1</code>", escaped)
        escaped = _BOLD_PATTERN.sub(r"<b>\1</b>", escaped)
        lines.append(escaped)

    return "\n".join(lines)


class TelegramGateway:
    """aiogram v3 bot gateway for Emergent."""

    def __init__(
        self,
        settings: EmergentSettings,
        runtime: AgentRuntime,
        store: MemoryStore,
        context_builder: ContextBuilder,
        notifier: ConsoleNotifier | None = None,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._store = store
        self._context_builder = context_builder
        self._notifier = notifier

        # Auth whitelist — frozenset, immutable at runtime
        self._allowed_ids: frozenset[int] = frozenset(settings.telegram.allowed_user_ids)

        self._bot = Bot(token=settings.telegram.bot_token)
        self._dp = Dispatcher()
        self._router = Router()
        self._dp.include_router(self._router)

        # Map session_id per Telegram user (chat_id)
        self._sessions: dict[int, str] = {}

        self._setup_handlers()

    async def _get_or_create_session(self, chat_id: int) -> str:
        if chat_id in self._sessions:
            return self._sessions[chat_id]
        # Try to restore from DB (survives restarts)
        existing = await self._store.get_session_id(chat_id)
        if existing:
            self._sessions[chat_id] = existing
            return existing
        # New session
        session_id = str(uuid.uuid4())
        self._sessions[chat_id] = session_id
        await self._store.save_session_mapping(chat_id, session_id)
        return session_id

    def _setup_handlers(self) -> None:
        @self._router.message(CommandStart())
        async def cmd_start(message: Message) -> None:
            if not self._check_auth(message):
                return
            await message.answer(
                "Hola! Soy Emergent, tu agente autónomo personal. ¿En qué puedo ayudarte?"
            )

        @self._router.message()
        async def handle_message(message: Message) -> None:
            if not self._check_auth(message):
                return
            await self._process_message(message)

        @self._router.callback_query()
        async def handle_callback(callback: CallbackQuery) -> None:
            await self._handle_confirmation_callback(callback)

    def _check_auth(self, message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else None
        if user_id not in self._allowed_ids:
            logger.warning(
                "auth_denied",
                user_id=user_id,
                username=message.from_user.username if message.from_user else None,
            )
            return False
        return True

    async def _process_message(self, message: Message) -> None:
        chat_id = message.chat.id
        user_text = message.text or ""

        if not user_text.strip():
            return

        session_id = await self._get_or_create_session(chat_id)
        log = logger.bind(chat_id=chat_id, session_id=session_id)
        log.info("telegram_message_received", message_len=len(user_text))

        if self._notifier:
            user = message.from_user
            username = (user.username or str(chat_id)) if user else str(chat_id)
            preview = user_text[:30] + ("..." if len(user_text) > 30 else "")
            self._notifier.message_received(username, preview, len(user_text))

        status = await self._status_start(chat_id=chat_id, text=_STATUS_HISTORY_ACTIVATED)
        status_animation_task = asyncio.create_task(self._run_status_animation(status))

        # Send typing indicator
        await self._bot.send_chat_action(chat_id=chat_id, action="typing")

        # Build context from memory
        try:
            profile_text, memories, summary, history = await self._context_builder.build_context(
                session_id=session_id,
                current_query=user_text,
            )
        except Exception as e:
            log.error("context_build_failed", error=str(e))
            profile_text, memories, summary, history = None, None, None, []

        # Check if summarization is needed
        if self._context_builder.should_summarize(history):
            try:
                client = create_llm_client(self._settings, self._settings.agent.summary_provider)
                try:
                    new_summary = await summarize_conversation(
                        client, history, summary_model=self._settings.agent.haiku_model
                    )
                finally:
                    await client.close()
                if new_summary:
                    await self._store.save_session_summary(session_id, new_summary)
                    summary = new_summary
                    # Keep only last 5 turns after summarization
                    history = history[-5:]
                    log.info("auto_summarization_done", summary_len=len(new_summary))
            except Exception as e:
                log.error("auto_summarization_failed", error=str(e))

        # Run agent with confirmation callback
        async def confirm_callback(tool_name: str, command_preview: str) -> bool:
            return await self._request_tier2_confirmation(
                chat_id=chat_id,
                tool_name=tool_name,
                command_preview=command_preview,
                status=status,
            )

        t0 = time.monotonic()
        try:
            response_text, trace_data = await self._runtime.run(
                user_message=user_text,
                session_id=session_id,
                history=history,
                user_profile=profile_text,
                semantic_memories=memories,
                session_summary=summary,
                confirm_callback=confirm_callback,
            )
            if self._notifier:
                duration = time.monotonic() - t0
                tokens = trace_data.get("total_tokens", 0) if isinstance(trace_data, dict) else 0
                self._notifier.message_sent(duration, tokens)
        except Exception as e:
            log.error("runtime_error", error=str(e))
            if self._notifier:
                self._notifier.error(str(e))
            await self._stop_status_animation(status_animation_task)
            await self._status_update(status, _STATUS_ERROR)
            response_text = "Ocurrió un error inesperado. Por favor, intentá de nuevo."
            trace_data = {}
        else:
            await self._stop_status_animation(status_animation_task)

        # Persist conversation
        try:
            await self._store.save_conversation_turn(session_id, "user", user_text)
            await self._store.save_conversation_turn(session_id, "assistant", response_text)
            await self._store.save_trace(trace_data)
        except Exception as e:
            log.error("persistence_failed", error=str(e))

        # Index new turns into ChromaDB (fire-and-forget, non-blocking)
        asyncio.create_task(
            self._context_builder._retriever.upsert_session(
                session_id=session_id,
                turns=[
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": response_text},
                ],
            )
        )

        # Send response (with chunking if needed)
        await self._status_update(status, _STATUS_READY)
        await self._send_response(chat_id=chat_id, text=response_text)

    async def _request_tier2_confirmation(
        self,
        chat_id: int,
        tool_name: str,
        command_preview: str,
        status: _StatusHandle | None = None,
    ) -> bool:
        """Send inline keyboard for TIER_2 confirmation and wait for response."""
        if status is not None:
            status.paused = True
            await self._status_update(status, _STATUS_CONFIRM)

        callback_key = str(uuid.uuid4())[:8]
        event = asyncio.Event()
        _pending_confirmations[callback_key] = event
        _confirmation_results[callback_key] = False

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Ejecutar",
                        callback_data=f"confirm_yes_{callback_key}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Cancelar",
                        callback_data=f"confirm_no_{callback_key}",
                    ),
                ]
            ]
        )

        await self._bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚖️ *Protocolo DeLorean*\n\n"
                f"Tool: `{tool_name}`\n"
                f"Comando: `{command_preview}`\n\n"
                f"_60 segundos para responder._"
            ),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

        try:
            await asyncio.wait_for(event.wait(), timeout=60)
            result = _confirmation_results.get(callback_key, False)
        except TimeoutError:
            result = False
            await self._bot.send_message(
                chat_id=chat_id, text="⏱️ Tiempo agotado. Operación cancelada."
            )
        finally:
            _pending_confirmations.pop(callback_key, None)
            _confirmation_results.pop(callback_key, None)
            if status is not None:
                status.paused = False

        return result

    async def _handle_confirmation_callback(self, callback: CallbackQuery) -> None:
        """Handle inline keyboard button presses."""
        data = callback.data or ""

        if data.startswith("confirm_yes_"):
            key = data[len("confirm_yes_") :]
            approved = True
        elif data.startswith("confirm_no_"):
            key = data[len("confirm_no_") :]
            approved = False
        else:
            await callback.answer("Callback desconocido")
            return

        event = _pending_confirmations.get(key)
        if event:
            _confirmation_results[key] = approved
            event.set()
            await callback.answer("✅ Aprobado" if approved else "❌ Cancelado")
            await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
            status = "aprobada ✅" if approved else "cancelada ❌"
            await callback.message.answer(f"Operación {status}.")  # type: ignore[union-attr]
        else:
            await callback.answer("Esta confirmación ya expiró.")

    async def _send_response(self, chat_id: int, text: str) -> None:
        """Send response, chunking if it exceeds Telegram's limit."""
        if not text:
            text = "(sin respuesta)"

        chunks = _split_message(text)
        for chunk in chunks:
            formatted = _format_chunk_for_telegram(chunk)
            if len(formatted) > MAX_MESSAGE_LENGTH:
                formatted = html.escape(chunk, quote=False)
            try:
                await self._bot.send_message(chat_id=chat_id, text=formatted, parse_mode="HTML")
            except Exception as e:
                logger.warning("telegram_send_html_failed", chat_id=chat_id, error=str(e))
                try:
                    await self._bot.send_message(chat_id=chat_id, text=chunk)
                except Exception as fallback_error:
                    logger.error(
                        "telegram_send_failed",
                        chat_id=chat_id,
                        error=str(fallback_error),
                    )

    async def _status_start(self, chat_id: int, text: str) -> _StatusHandle:
        """Create a single status message that can be updated during the request."""
        try:
            sent = await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.error("telegram_status_start_failed", chat_id=chat_id, error=str(e))
            return _StatusHandle(chat_id=chat_id, message_id=None, last_text="")

        message_id_raw = getattr(sent, "message_id", None)
        message_id = message_id_raw if isinstance(message_id_raw, int) else None
        return _StatusHandle(chat_id=chat_id, message_id=message_id, last_text=text)

    async def _status_update(self, status: _StatusHandle, text: str) -> None:
        """Update status message and fallback to a new message if edit fails."""
        if text == status.last_text:
            return

        if status.message_id is not None:
            try:
                await self._bot.edit_message_text(
                    chat_id=status.chat_id,
                    message_id=status.message_id,
                    text=text,
                )
                status.last_text = text
                return
            except Exception as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    status.last_text = text
                    return
                logger.warning(
                    "telegram_status_edit_failed",
                    chat_id=status.chat_id,
                    message_id=status.message_id,
                    error=str(e),
                )

        try:
            sent = await self._bot.send_message(chat_id=status.chat_id, text=text)
            message_id_raw = getattr(sent, "message_id", None)
            if isinstance(message_id_raw, int):
                status.message_id = message_id_raw
            status.last_text = text
        except Exception as e:
            logger.error("telegram_status_fallback_failed", chat_id=status.chat_id, error=str(e))

    async def _run_status_animation(
        self,
        status: _StatusHandle,
        interval_seconds: float = _STATUS_ANIMATION_INTERVAL_SECONDS,
    ) -> None:
        """Continuously rotate status text while a request is running."""
        index = 0
        while True:
            await asyncio.sleep(interval_seconds)
            if status.paused:
                continue
            frame = _STATUS_ANIMATION_FRAMES[index]
            index = (index + 1) % len(_STATUS_ANIMATION_FRAMES)
            await self._status_update(status, frame)

    async def _stop_status_animation(self, task: asyncio.Task[None]) -> None:
        """Cancel background status animation safely."""
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    async def start(self) -> None:
        """Start the bot with long polling."""
        logger.info("telegram_bot_starting")
        await self._dp.start_polling(self._bot)

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("telegram_bot_stopping")
        await self._bot.session.close()
