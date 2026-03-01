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
- Terminal channel via `rich`
- Persistent cron scheduler via `APScheduler`
- Persistence via SQLite (WAL) and ChromaDB
- Observability via `structlog` (JSON logs)

## Quick Commands
- Run agent: `uv run python -m emergent`
- Run tests: `uv run pytest`
- Run tests without expensive E2E: `uv run pytest tests/test_e2e/ -k "not expensive"`
- Run security tests: `uv run pytest -m security`
- Lint: `uv run ruff check src/`
- Format: `uv run ruff format src/`
- Type check: `uv run mypy src/`
- Alternate run: `make run`
- Alternate tests (skip E2E): `make test`
- All tests: `make test-all`
- Lint + format check: `make lint`
- Auto-fix lint/format: `make format`
- Type check: `make typecheck`
- Open observability dashboard: `make dashboard`
- Triage view: `make triage`

## Architecture Notes
- Core dependency flow (keep this direction):
  `terminal.py / telegram.py -> runtime.py -> context.py + registry.py -> tools/*.py + memory/*.py -> tracing.py`
- `runtime.py` is the central control loop.
- Tool execution always goes through the tool registry and safety checks.
- `context.py` assembles the token-budgeted context window (profile + memories + summary + recent turns).
- `prompts.py` builds the system prompt injected into every session.

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
- `src/emergent/__init__.py`: `EmergentError` base exception hierarchy
- `src/emergent/__main__.py`: entry point — wires channels, registry, runtime, scheduler
- `src/emergent/config.py`: settings loading (`.env` + `config.yaml`); `AgentConfig`; `verify_guards_integrity()`
- `src/emergent/agent/runtime.py`: core agent loop and orchestration (`AgentRuntime`)
- `src/emergent/agent/context.py`: `ContextBuilder` — token-budgeted context assembly
- `src/emergent/agent/prompts.py`: system prompt builders
- `src/emergent/tools/registry.py`: `ToolDefinition`, `ToolRegistry`, `SafetyTier`, `classify_command()`
- `src/emergent/tools/shell.py`: shell tool execution + safety classifier
- `src/emergent/tools/files.py`: 8 file tools (read/write/list/tree/search/info/move/delete)
- `src/emergent/tools/web.py`: `web_fetch` (SSRF-protected)
- `src/emergent/tools/system_info.py`: `system_info` tool
- `src/emergent/tools/memory_tools.py`: `memory_search` + `memory_store` tool handlers
- `src/emergent/tools/cron.py`: `cron_schedule` tool + APScheduler initialization
- `src/emergent/channels/terminal.py`: interactive rich REPL with live dashboard and skill presets
- `src/emergent/channels/telegram.py`: Telegram gateway (`aiogram` v3), per-user session persistence
- `src/emergent/llm/client.py`: `LLMClient` protocol — provider-agnostic interface
- `src/emergent/llm/models.py`: `LLMResponse`, `LLMTextBlock`, `LLMToolUseBlock`, `LLMUsage` dataclasses
- `src/emergent/llm/factory.py`: `create_llm_client()` factory
- `src/emergent/llm/anthropic_client.py`: `AnthropicLLMClient`
- `src/emergent/llm/ollama_client.py`: `OllamaLLMClient`
- `src/emergent/memory/store.py`: SQLite WAL CRUD for memory state (8 tables)
- `src/emergent/memory/retriever.py`: `SemanticRetriever` — ChromaDB cosine similarity search
- `src/emergent/memory/summarizer.py`: `summarize_conversation()` using Haiku model
- `src/emergent/observability/tracing.py`: `configure_logging()`, `trace_span()` context manager
- `src/emergent/observability/banner.py`: `print_banner()`, `ConsoleNotifier`
- `src/emergent/observability/metrics.py`: `print_dashboard()`, `print_triage()` CLI views

## WIP / Incomplete Modules (do not invoke in production)
- `src/emergent/social/worker.py`: `SocialWorker` — drafts X posts from research findings. References `settings.social.*` which is **not defined** in `config.py`; will raise `AttributeError` if `run()` is called.
- `src/emergent/social/x_client.py`: `XClient` — X API v2 wrapper. Requires `X_ACCESS_TOKEN` env var not present in `.env.example`.
- `src/emergent/tools/social.py`: `social_draft` and `social_publish` tool definitions. **Not registered** in `create_registry()` or `__main__.py`. Do not register them until `SocialConfig` is added to `config.py` and `X_ACCESS_TOKEN` is documented.

## Recommended LLM Workflow
1. Read relevant files before changing behavior.
2. Check current docs for any external API with Context7.
3. Make minimal, focused edits that preserve module dependency direction.
4. Run lint, type checks, and targeted tests after changes.
5. Do not weaken safety checks, guardrails, or secret-handling rules.
