"""Tests for MemoryStore — SQLite persistence."""

from __future__ import annotations

from emergent.memory.store import MemoryStore


class TestMemoryStore:
    async def test_save_and_get_conversation(self, tmp_db: MemoryStore):
        await tmp_db.save_conversation_turn("session1", "user", "Hello")
        await tmp_db.save_conversation_turn("session1", "assistant", "Hi there!")

        history = await tmp_db.get_recent_history("session1")
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Hello"

    async def test_session_isolation(self, tmp_db: MemoryStore):
        await tmp_db.save_conversation_turn("session1", "user", "A")
        await tmp_db.save_conversation_turn("session2", "user", "B")

        history1 = await tmp_db.get_recent_history("session1")
        history2 = await tmp_db.get_recent_history("session2")

        assert len(history1) == 1
        assert len(history2) == 1
        assert history1[0]["content"] == "A"
        assert history2[0]["content"] == "B"

    async def test_max_turns_respected(self, tmp_db: MemoryStore):
        for i in range(30):
            await tmp_db.save_conversation_turn("session1", "user", f"msg {i}")

        history = await tmp_db.get_recent_history("session1", max_turns=10)
        assert len(history) == 10

    async def test_user_profile_set_and_get(self, tmp_db: MemoryStore):
        await tmp_db.set_profile_key("editor", "neovim", confidence=1.0)
        profile = await tmp_db.get_user_profile()
        assert profile.get("editor") == "neovim"

    async def test_profile_confidence_filter(self, tmp_db: MemoryStore):
        await tmp_db.set_profile_key("low_confidence_key", "value", confidence=0.3)
        await tmp_db.set_profile_key("high_confidence_key", "value", confidence=0.8)

        profile = await tmp_db.get_user_profile(min_confidence=0.5)
        assert "low_confidence_key" not in profile
        assert "high_confidence_key" in profile

    async def test_profile_not_overwritten_by_lower_confidence(self, tmp_db: MemoryStore):
        await tmp_db.set_profile_key("editor", "neovim", confidence=0.9)
        await tmp_db.set_profile_key("editor", "vim", confidence=0.8)  # lower, should not overwrite

        profile = await tmp_db.get_user_profile()
        assert profile.get("editor") == "neovim"

    async def test_save_and_get_session_summary(self, tmp_db: MemoryStore):
        await tmp_db.save_session_summary("session1", "We discussed Python async patterns.")
        summary = await tmp_db.get_session_summary("session1")
        assert summary is not None
        assert "async" in summary

    async def test_save_trace(self, tmp_db: MemoryStore):
        trace = {
            "trace_id": "test-trace-1",
            "session_id": "session1",
            "duration_ms": 1500,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "total_cost_usd": 0.001,
            "iterations": 2,
            "tools_called": ["shell_execute"],
            "success": True,
            "error_message": None,
        }
        await tmp_db.save_trace(trace)

    async def test_profile_as_text(self, tmp_db: MemoryStore):
        await tmp_db.set_profile_key("editor", "neovim")
        await tmp_db.set_profile_key("language", "python")

        text = await tmp_db.get_profile_as_text()
        assert text is not None
        assert "editor" in text
        assert "neovim" in text

    async def test_persistence_across_instances(self, tmp_path):
        """Data should persist across MemoryStore instances (same db file)."""
        db_path = tmp_path / "persist_test.db"

        store1 = MemoryStore(db_path)
        await store1.save_conversation_turn("session1", "user", "Persistent message")
        await store1.close()

        store2 = MemoryStore(db_path)
        history = await store2.get_recent_history("session1")
        assert len(history) == 1
        assert history[0]["content"] == "Persistent message"
        await store2.close()

    async def test_research_findings_crud(self, tmp_db: MemoryStore):
        inserted = await tmp_db.save_research_findings(
            [
                {
                    "id": "f1",
                    "run_id": "r1",
                    "domain": "agent_architecture",
                    "source": "github",
                    "title": "agent runtime release",
                    "url": "https://example.com/a",
                    "summary": "details",
                    "relevance_score": 0.8,
                    "is_highlight": True,
                    "published_at": "2026-02-22T00:00:00+00:00",
                    "metadata": {"stars": 100},
                }
            ]
        )
        assert inserted == 1

        highlights = await tmp_db.get_research_highlights(days=30)
        assert len(highlights) == 1
        by_domain = await tmp_db.get_research_by_domain("agent_architecture")
        assert len(by_domain) == 1
        search = await tmp_db.search_research("runtime")
        assert len(search) == 1

    async def test_auto_name_session_from_first_prompt(self, tmp_db: MemoryStore):
        name = await tmp_db.auto_name_session_from_first_prompt(
            "session-1",
            "Necesito ayuda para arreglar el login en Telegram",
            channel="terminal",
        )
        assert name.startswith("necesito-ayuda-para-arreglar")

        name_again = await tmp_db.auto_name_session_from_first_prompt(
            "session-1",
            "Otro prompt distinto",
            channel="terminal",
        )
        assert name_again == name

    async def test_get_all_sessions_with_names(self, tmp_db: MemoryStore):
        await tmp_db.auto_name_session_from_first_prompt("session-1", "debug login telegram")
        await tmp_db.save_conversation_turn("session-1", "user", "hola")

        sessions = await tmp_db.get_all_sessions_with_names(limit=5)
        assert sessions[0]["session_id"] == "session-1"
        assert sessions[0]["display_name"] == "debug-login-telegram"

    async def test_list_chat_sessions_with_names(self, tmp_db: MemoryStore):
        await tmp_db.save_session_mapping(1, "session-a")
        await tmp_db.save_session_mapping(1, "session-b")
        await tmp_db.auto_name_session_from_first_prompt("session-a", "fix auth")

        sessions = await tmp_db.list_chat_sessions_with_names(1, limit=10)
        assert sessions[0]["session_id"] == "session-b"
        assert sessions[1]["display_name"] == "fix-auth"
