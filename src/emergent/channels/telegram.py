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
    BufferedInputFile,
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

_STATUS_HISTORY_ACTIVATED = "Iniciando solicitud..."
_STATUS_ANIMATION_FRAMES = (
    "Procesando solicitud.",
    "Procesando solicitud..",
    "Procesando solicitud...",
    "Consultando contexto...",
    "Preparando respuesta...",
)
_STATUS_ANIMATION_INTERVAL_SECONDS = 2.8
_STATUS_CONFIRM = "Se requiere autorización para continuar."
_STATUS_READY = "Solicitud completada."
_STATUS_ERROR = "Ocurrió un error. Intenta nuevamente."

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
_FENCED_CODE_BLOCK_PATTERN = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+\-#.]+)?[ \t]*\n(?P<code>.*?)(?:\n)?```",
    re.DOTALL,
)
_SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")
_LONG_CODE_BLOCK_MIN_LINES = 25
_LONG_CODE_BLOCK_MIN_CHARS = 1500


@dataclass
class _CodeAttachment:
    filename: str
    content: str
    language: str | None
    line_count: int


def _format_plain_text_for_telegram(text: str) -> str:
    """Convert plain markdown-like lines to Telegram HTML subset."""
    lines: list[str] = []
    for raw_line in text.splitlines():
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


def _normalize_language(language: str | None) -> str | None:
    """Normalize fenced language labels for display and file extension mapping."""
    if not language:
        return None
    normalized = re.sub(r"[^a-z0-9_+\-.#]", "", language.strip().lower())
    return normalized or None


def _extension_for_language(language: str | None) -> str:
    """Infer a file extension from fenced code language."""
    mapping = {
        "bash": "sh",
        "console": "txt",
        "css": "css",
        "dockerfile": "Dockerfile",
        "go": "go",
        "html": "html",
        "java": "java",
        "javascript": "js",
        "js": "js",
        "json": "json",
        "jsonl": "jsonl",
        "markdown": "md",
        "md": "md",
        "py": "py",
        "python": "py",
        "sh": "sh",
        "shell": "sh",
        "sql": "sql",
        "text": "txt",
        "toml": "toml",
        "ts": "ts",
        "tsx": "tsx",
        "txt": "txt",
        "xml": "xml",
        "yaml": "yaml",
        "yml": "yml",
    }
    if language == "dockerfile":
        return "Dockerfile"
    return mapping.get(language or "", "txt")


def _extract_long_code_blocks(text: str) -> tuple[str, list[_CodeAttachment]]:
    """Replace long fenced blocks with placeholders and collect attachments."""
    attachments: list[_CodeAttachment] = []
    parts: list[str] = []
    cursor = 0
    attachment_index = 1

    for match in _FENCED_CODE_BLOCK_PATTERN.finditer(text):
        parts.append(text[cursor : match.start()])
        code = (match.group("code") or "").strip("\n")
        line_count = code.count("\n") + 1 if code else 0
        is_long = (
            line_count >= _LONG_CODE_BLOCK_MIN_LINES or len(code) >= _LONG_CODE_BLOCK_MIN_CHARS
        )

        if is_long:
            language = _normalize_language(match.group("lang"))
            extension = _extension_for_language(language)
            filename = (
                f"snippet_{attachment_index}.Dockerfile"
                if extension == "Dockerfile"
                else f"snippet_{attachment_index}.{extension}"
            )
            attachments.append(
                _CodeAttachment(
                    filename=filename,
                    content=code,
                    language=language,
                    line_count=line_count,
                )
            )
            language_suffix = f" ({language})" if language else ""
            parts.append(
                f"\nBloque de codigo adjunto: `{filename}`{language_suffix}. "
                f"({line_count} lineas)\n"
            )
            attachment_index += 1
        else:
            parts.append(match.group(0))

        cursor = match.end()

    parts.append(text[cursor:])
    return "".join(parts), attachments


def _format_chunk_for_telegram(chunk: str) -> str:
    """Convert a markdown-like chunk to Telegram HTML subset."""
    formatted_parts: list[str] = []
    cursor = 0

    for match in _FENCED_CODE_BLOCK_PATTERN.finditer(chunk):
        plain = chunk[cursor : match.start()]
        if plain:
            formatted_parts.append(_format_plain_text_for_telegram(plain))

        language = _normalize_language(match.group("lang"))
        if language:
            language_html = html.escape(language, quote=False)
            formatted_parts.append(f"<b>Lenguaje:</b> <code>{language_html}</code>")

        code = (match.group("code") or "").strip("\n")
        escaped_code = html.escape(code, quote=False)
        formatted_parts.append(f"<pre><code>{escaped_code}</code></pre>")
        cursor = match.end()

    tail = chunk[cursor:]
    if tail:
        formatted_parts.append(_format_plain_text_for_telegram(tail))

    return "\n".join(part for part in formatted_parts if part)


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
        self._pending_session_picks: dict[str, tuple[int, str]] = {}

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
        await self._store.ensure_session(session_id, channel="telegram")
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

        if await self._handle_session_command(message):
            return

        session_id = await self._get_or_create_session(chat_id)
        log = logger.bind(chat_id=chat_id, session_id=session_id)
        log.info("telegram_message_received", message_len=len(user_text))

        try:
            await self._store.auto_name_session_from_first_prompt(
                session_id,
                user_text,
                channel="telegram",
            )
        except Exception as e:
            log.warning("telegram_session_autoname_failed", error=str(e))

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
                    keep_turns = getattr(self._context_builder, "history_keep_after_summary", 5)
                    history = history[-keep_turns:]
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
            await self._store.save_conversation_turn(
                session_id,
                "user",
                user_text,
                model=self._settings.agent.model,
            )
            await self._store.save_conversation_turn(
                session_id,
                "assistant",
                response_text,
                model=self._settings.agent.model,
            )
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
                        text="Autorizar",
                        callback_data=f"confirm_yes_{callback_key}",
                    ),
                    InlineKeyboardButton(
                        text="Cancelar",
                        callback_data=f"confirm_no_{callback_key}",
                    ),
                ]
            ]
        )

        await self._bot.send_message(
            chat_id=chat_id,
            text=(
                f"*Autorización requerida*\n\n"
                f"Herramienta: `{tool_name}`\n"
                f"Acción: `{command_preview}`\n\n"
                f"_Tienes 60 segundos para responder._"
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
                chat_id=chat_id,
                text="Tiempo de espera agotado. Operación cancelada.",
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

        if data.startswith("session_pick_"):
            await self._handle_session_pick_callback(callback)
            return

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
            await callback.answer("Autorizado" if approved else "Cancelado")
            await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
            status = "autorizada" if approved else "cancelada"
            await callback.message.answer(f"Operación {status}.")  # type: ignore[union-attr]
        else:
            await callback.answer("Esta confirmación ya expiró.")

    async def _handle_session_command(self, message: Message) -> bool:
        """Handle /session command in Telegram chat."""
        raw = (message.text or "").strip()
        if not raw:
            return False

        parts = raw.split(maxsplit=1)
        command = parts[0].lower()
        if not command.startswith("/session"):
            return False

        chat_id = message.chat.id
        current_session = await self._get_or_create_session(chat_id)

        if len(parts) > 1:
            arg = parts[1].strip()
            if arg.lower() in {"new", "nuevo", "reset", "default"}:
                session_id = str(uuid.uuid4())
                self._sessions[chat_id] = session_id
                await self._store.save_session_mapping(chat_id, session_id)
                await self._store.ensure_session(session_id, channel="telegram")
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=f"Nueva sesión: `{session_id}`",
                    parse_mode="Markdown",
                )
                return True

            if not _SESSION_ID_PATTERN.match(arg):
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=("Session id inválido. Usa 3-64 caracteres (letras, números, '_' o '-')."),
                )
                return True

            self._sessions[chat_id] = arg
            await self._store.save_session_mapping(chat_id, arg)
            await self._store.ensure_session(arg, channel="telegram")
            await self._bot.send_message(
                chat_id=chat_id,
                text=f"Sesión activa: `{arg}`",
                parse_mode="Markdown",
            )
            return True

        sessions = await self._store.list_chat_sessions_with_names(chat_id, limit=10)

        buttons: list[list[InlineKeyboardButton]] = []
        for item in sessions:
            session_id = str(item["session_id"])
            display_name = str(item["display_name"])
            key = str(uuid.uuid4())[:8]
            self._pending_session_picks[key] = (chat_id, session_id)
            prefix = "[actual] " if session_id == current_session else "[historial] "
            short_id = session_id[:8]
            name = display_name if display_name != session_id else f"{short_id}..."
            label = f"{prefix}{name[:28]}"
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"session_pick_{key}",
                    )
                ]
            )

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await self._bot.send_message(
            chat_id=chat_id,
            text=(
                "Sesiones disponibles\n"
                "Elegí una para volver a ese contexto.\n\n"
                f"Actual: `{current_session}`"
            ),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return True

    async def _handle_session_pick_callback(self, callback: CallbackQuery) -> None:
        """Activate a previously used session from inline picker."""
        data = callback.data or ""
        key = data[len("session_pick_") :]
        payload = self._pending_session_picks.pop(key, None)
        if payload is None:
            await callback.answer("Esta selección ya expiró.")
            return

        callback_chat_id = callback.message.chat.id if callback.message else None
        if not isinstance(callback_chat_id, int):
            await callback.answer("No pude identificar el chat.")
            return

        owner_chat_id, session_id = payload
        if owner_chat_id != callback_chat_id:
            await callback.answer("Esta sesión no pertenece a este chat.")
            return

        self._sessions[callback_chat_id] = session_id
        await self._store.save_session_mapping(callback_chat_id, session_id)
        await callback.answer("Sesión activada")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
            await callback.message.answer(
                f"Volvimos a la sesión `{session_id}`", parse_mode="Markdown"
            )

    async def _send_response(self, chat_id: int, text: str) -> None:
        """Send response, chunking if it exceeds Telegram's limit."""
        if not text:
            text = "(sin respuesta)"

        text, attachments = _extract_long_code_blocks(text)

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

        for attachment in attachments:
            payload = attachment.content.encode("utf-8")
            caption = (
                f"Bloque de codigo adjunto ({attachment.line_count} lineas)."
                if not attachment.language
                else (
                    "Bloque de codigo adjunto "
                    f"({attachment.language}, {attachment.line_count} lineas)."
                )
            )
            try:
                await self._bot.send_document(
                    chat_id=chat_id,
                    document=BufferedInputFile(payload, filename=attachment.filename),
                    caption=caption,
                )
            except Exception as e:
                logger.warning(
                    "telegram_send_document_failed",
                    chat_id=chat_id,
                    filename=attachment.filename,
                    error=str(e),
                )
                fallback_header = f"Adjunto no disponible: {attachment.filename}"
                fallback_text = f"{fallback_header}\n\n{attachment.content}"
                for fallback_chunk in _split_message(fallback_text):
                    await self._bot.send_message(chat_id=chat_id, text=fallback_chunk)

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
