"""SQLite persistence layer — source of truth (L0)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
    content TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    tokens_used INTEGER,
    model TEXT
);

CREATE TABLE IF NOT EXISTS tool_executions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_text TEXT,
    safety_tier TEXT,
    user_confirmed BOOLEAN,
    duration_ms INTEGER,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    total_duration_ms INTEGER,
    total_tokens INTEGER,
    total_cost_usd REAL,
    iterations INTEGER,
    tools_called_json TEXT,
    success BOOLEAN,
    error_message TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_span_id TEXT,
    event_type TEXT NOT NULL,
    timestamp_start REAL NOT NULL,
    duration_ms REAL,
    metadata_json TEXT,
    error TEXT,
    FOREIGN KEY (trace_id) REFERENCES traces(id)
);

CREATE TABLE IF NOT EXISTS session_summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    key_topics_json TEXT,
    generated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    chat_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_timestamp ON conversations(timestamp);
CREATE INDEX IF NOT EXISTS idx_traces_timestamp ON traces(timestamp);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_error ON spans(error) WHERE error IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_profile_confidence ON user_profile(confidence);
"""


class MemoryStore:
    """Async wrapper around SQLite for all persistence operations."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        return self._conn

    async def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Execute SQL in a thread pool to avoid blocking the event loop."""
        loop = asyncio.get_event_loop()

        def _run() -> list[sqlite3.Row]:
            conn = self._get_conn()
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.fetchall()

        async with self._lock:
            return await loop.run_in_executor(None, _run)

    async def _executemany(self, sql: str, params_list: list[tuple[Any, ...]]) -> None:
        loop = asyncio.get_event_loop()

        def _run() -> None:
            conn = self._get_conn()
            conn.executemany(sql, params_list)
            conn.commit()

        async with self._lock:
            await loop.run_in_executor(None, _run)

    # --- Conversations ---

    async def save_conversation_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        tokens_used: int | None = None,
        model: str | None = None,
    ) -> str:
        turn_id = str(uuid.uuid4())
        await self._execute(
            "INSERT INTO conversations (id, session_id, role, content, tokens_used, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (turn_id, session_id, role, content, tokens_used, model),
        )
        return turn_id

    async def get_recent_history(
        self, session_id: str, max_turns: int = 20
    ) -> list[dict[str, Any]]:
        rows = await self._execute(
            "SELECT role, content FROM conversations "
            "WHERE session_id = ? ORDER BY rowid DESC LIMIT ?",
            (session_id, max_turns),
        )
        # Return in chronological order
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def get_all_sessions(self) -> list[str]:
        rows = await self._execute(
            "SELECT DISTINCT session_id FROM conversations ORDER BY MIN(timestamp) DESC"
        )
        return [r["session_id"] for r in rows]

    # --- Traces ---

    async def save_trace(self, trace_data: dict[str, Any]) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO traces "
            "(id, session_id, total_duration_ms, total_tokens, total_cost_usd, "
            "iterations, tools_called_json, success, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace_data.get("trace_id", str(uuid.uuid4())),
                trace_data.get("session_id", ""),
                trace_data.get("duration_ms"),
                trace_data.get("total_input_tokens", 0) + trace_data.get("total_output_tokens", 0),
                trace_data.get("total_cost_usd"),
                trace_data.get("iterations"),
                json.dumps(trace_data.get("tools_called", [])),
                trace_data.get("success", True),
                trace_data.get("error_message"),
            ),
        )

    # --- User Profile ---

    async def get_user_profile(self, min_confidence: float = 0.5) -> dict[str, str]:
        rows = await self._execute(
            "SELECT key, value FROM user_profile WHERE confidence >= ? ORDER BY confidence DESC",
            (min_confidence,),
        )
        return {r["key"]: r["value"] for r in rows}

    async def set_profile_key(self, key: str, value: str, confidence: float = 1.0) -> None:
        # Only overwrite if new confidence is significantly higher
        existing = await self._execute("SELECT confidence FROM user_profile WHERE key = ?", (key,))
        if existing:
            existing_confidence = existing[0]["confidence"]
            if confidence <= existing_confidence + 0.1:
                logger.debug("profile_not_updated_lower_confidence", key=key)
                return

        await self._execute(
            "INSERT OR REPLACE INTO user_profile (key, value, confidence, updated_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (key, value, confidence),
        )

    async def get_profile_as_text(self, min_confidence: float = 0.5) -> str | None:
        profile = await self.get_user_profile(min_confidence=min_confidence)
        if not profile:
            return None
        lines = [f"- {k}: {v}" for k, v in profile.items()]
        return "\n".join(lines)

    # --- Session Summaries ---

    async def save_session_summary(
        self, session_id: str, summary: str, key_topics: list[str] | None = None
    ) -> None:
        await self._execute(
            "INSERT INTO session_summaries (id, session_id, summary, key_topics_json) "
            "VALUES (?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                session_id,
                summary,
                json.dumps(key_topics or []),
            ),
        )

    async def get_session_summary(self, session_id: str) -> str | None:
        rows = await self._execute(
            "SELECT summary FROM session_summaries WHERE session_id = ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (session_id,),
        )
        return rows[0]["summary"] if rows else None

    # --- Tool Executions ---

    async def save_tool_execution(
        self,
        session_id: str,
        tool_name: str,
        input_preview: str,
        output_text: str | None,
        safety_tier: str,
        user_confirmed: bool | None,
        duration_ms: int | None,
    ) -> None:
        await self._execute(
            "INSERT INTO tool_executions "
            "(id, session_id, tool_name, input_json, output_text, "
            "safety_tier, user_confirmed, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                session_id,
                tool_name,
                input_preview[:100],  # sanitized: only first 100 chars
                output_text[:500] if output_text else None,
                safety_tier,
                user_confirmed,
                duration_ms,
            ),
        )

    # --- Chat Session Mapping ---

    async def save_session_mapping(self, chat_id: int, session_id: str) -> None:
        """Persist chat_id → session_id so sessions survive restarts."""
        await self._execute(
            "INSERT OR REPLACE INTO chat_sessions (chat_id, session_id, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (chat_id, session_id),
        )

    async def get_session_id(self, chat_id: int) -> str | None:
        rows = await self._execute(
            "SELECT session_id FROM chat_sessions WHERE chat_id = ?",
            (chat_id,),
        )
        return rows[0]["session_id"] if rows else None

    # --- Cleanup (APScheduler job) ---

    async def cleanup_old_data(
        self, conversations_ttl_days: int = 90, traces_ttl_days: int = 30
    ) -> None:
        await self._execute(
            f"DELETE FROM conversations WHERE timestamp < datetime('now', '-{conversations_ttl_days} days')"
        )
        await self._execute(
            f"DELETE FROM traces WHERE timestamp < datetime('now', '-{traces_ttl_days} days')"
        )
        logger.info("cleanup_done", conv_ttl=conversations_ttl_days, trace_ttl=traces_ttl_days)

    async def decay_profile_confidence(self) -> None:
        """Monthly: decay confidence for old profile entries."""
        await self._execute(
            "UPDATE user_profile "
            "SET confidence = MAX(0.1, confidence - 0.05), updated_at = CURRENT_TIMESTAMP "
            "WHERE updated_at < datetime('now', '-30 days')"
        )
        await self._execute("DELETE FROM user_profile WHERE confidence < 0.1")
        logger.info("profile_confidence_decayed")

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
