# AGENTS.md - Instructions for Any LLM Contributor

This file defines the working rules for AI agents editing this repository.

## Project Summary
- Name: Emergent (autonomous agent runtime)
- Language: Python 3.12+
- Runtime style: full asyncio (async-first codebase)
- Package manager and task runner: `uv`

## What This Project Uses
- Custom agent loop built around Claude API `tool_use`
- No orchestration frameworks (do not add LangChain or LangGraph)
- Telegram channel via `aiogram` v3
- Persistence via SQLite and ChromaDB
- Observability via `structlog` (JSON logs)

## Quick Commands
- Run agent: `uv run python -m emergent`
- Run tests: `uv run pytest`
- Run tests without expensive E2E: `uv run pytest tests/test_e2e/ -k "not expensive"`
- Run security tests: `uv run pytest -m security`
- Lint: `uv run ruff check src/`
- Type check: `uv run mypy src/`
- Alternate run: `make run`
- Alternate tests (skip E2E): `make test`
- Open observability dashboard: `make dashboard`

## Architecture Notes
- Core dependency flow (keep this direction):
  `telegram.py -> runtime.py -> context.py + registry.py -> tools/*.py + memory/*.py -> tracing.py`
- `runtime.py` is the central control loop.
- Tool execution always goes through the tool registry and safety checks.

## Non-Negotiable Rules
- All I/O functions must be `async`.
- Every function signature must include type hints.
- Custom exceptions must inherit from `EmergentError` (`src/emergent/__init__.py`).
- Tools must be `ToolDefinition` dataclasses registered in `ToolRegistry`.
- Safety classifier must run before every tool execution (no bypass).
- Safety classifier must be deterministic pattern matching, never an LLM call.
- Never hardcode API keys, tokens, or secrets.
- Use Context7 to fetch up-to-date docs for external libraries before implementing changes.
- SQLite must run in WAL mode.
- `structlog` must output JSON.
- Runtime guards (`max_iterations`, `max_tokens`, timeouts) are fixed constants; agents must not make them user-modifiable.

## Key Files and Ownership
- `src/emergent/config.py`: settings loading (`.env` + `config.yaml`)
- `src/emergent/agent/runtime.py`: core agent loop and orchestration
- `src/emergent/tools/registry.py`: `ToolDefinition`, `ToolRegistry`, `SafetyTier`
- `src/emergent/tools/shell.py`: shell tool execution + safety classifier
- `src/emergent/channels/telegram.py`: Telegram gateway (`aiogram` v3)
- `src/emergent/memory/store.py`: SQLite CRUD for memory state
- `src/emergent/observability/tracing.py`: tracing events, spans, structured logging

## Recommended LLM Workflow
1. Read relevant files before changing behavior.
2. Check current docs for any external API with Context7.
3. Make minimal, focused edits that preserve module dependency direction.
4. Run lint, type checks, and targeted tests after changes.
5. Do not weaken safety checks, guardrails, or secret-handling rules.
6. When selecting models for production agents, use `$llm-agent-evaluation` for weighted comparisons and pass^k-based decisions.
