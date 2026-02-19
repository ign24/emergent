# Emergent — Autonomous Agent Runtime

Python 3.12+ project. Full asyncio. Package manager: uv.

## Commands
- `uv run python -m emergent` — Start agent
- `uv run pytest` — Tests
- `uv run pytest tests/test_e2e/ -k "not expensive"` — Skip costly E2E
- `uv run pytest -m security` — Security/red team tests
- `uv run ruff check src/` — Lint
- `uv run mypy src/` — Type check
- `make run` — Start agent
- `make test` — Run all tests (skip e2e)
- `make dashboard` — Show observability dashboard

## Architecture
Custom agent loop using Claude API tool_use. No frameworks (no LangChain/LangGraph).
aiogram v3 for Telegram. SQLite + ChromaDB for persistence. structlog for tracing.

## Module dependency order
telegram.py → runtime.py → context.py + registry.py → tools/*.py + memory/*.py → tracing.py

## Rules
- All I/O functions must be async
- Type hints on ALL function signatures
- Custom exceptions inherit from EmergentError (defined in emergent/__init__.py)
- Tools are ToolDefinition dataclasses registered via ToolRegistry
- Safety classifier runs BEFORE every tool execution — no exceptions
- Safety classifier is NEVER an LLM call — it is deterministic pattern matching only
- Never hardcode API keys or tokens anywhere
- Use context7 for current API docs of all libraries before implementing
- SQLite WAL mode always enabled
- structlog JSON renderer from day 1
- Guards (max_iterations, max_tokens, timeouts) are hardcoded — the agent cannot modify them

## Key files
- `src/emergent/config.py` — pydantic-settings: loads .env + config.yaml
- `src/emergent/agent/runtime.py` — THE core module: agentic loop
- `src/emergent/tools/registry.py` — ToolDefinition, ToolRegistry, SafetyTier
- `src/emergent/tools/shell.py` — Shell execute + safety classifier
- `src/emergent/channels/telegram.py` — aiogram v3 bot gateway
- `src/emergent/memory/store.py` — SQLite CRUD
- `src/emergent/observability/tracing.py` — TraceEvent, spans, structlog
