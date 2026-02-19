# Emergent: Engineering Specification
## Autonomous Agent Runtime — Complete Build Spec

*v0.3 — Febrero 2026*

---

## 0. Cómo Usar Este Documento

Este documento es la **especificación completa** para construir Emergent. Está diseñado para ser consumido por un LLM de código (Claude Code con Opus 4.5, o cualquier coding agent con capacidad de ejecución).

### Herramientas disponibles en el entorno de desarrollo

**Skills instaladas (`~/.claude/skills/`):**

| Skill | Cuándo usarla |
|---|---|
| `python-expert` | Para toda implementación Python. Patrones async, type hints, dataclasses, error handling. |
| `fullstack-developer` | Para la arquitectura general, integración de componentes, estructura de proyecto. |
| `code-reviewer` | Después de implementar cada módulo. Review de calidad, bugs, security issues. |
| `debugger` | Cuando algo falla. Diagnóstico sistemático, root cause analysis. |
| `e2e-testing` | Para tests de integración y end-to-end del agent loop completo. |
| `python-testing` | Para unit tests de cada módulo. pytest patterns, fixtures, mocks. |
| `test-driven-development` | Para escribir tests ANTES del código en componentes críticos (safety classifier, guards). |
| `security-review` | Review de seguridad del shell tool, input sanitization, auth. |
| `api-design` | Para el diseño del tool registry, interfaces entre módulos. |
| `backend-patterns` | Patterns de persistencia (SQLite), async I/O, error handling. |
| `docker-patterns` | Para el sandbox de ejecución de comandos (Phase 2). |
| `deployment-patterns` | Para systemd service, VPS migration. |
| `project-planner` | Para descomponer cada Phase en tareas ejecutables. |
| `mcp-builder` | Si necesitamos exponer tools como MCP server. |
| `data-analyst` | Para el módulo de observability y métricas. |

**MCP servers configurados:**

| MCP | Uso |
|---|---|
| `context7` | **Usar siempre antes de implementar cualquier módulo.** Obtener docs actualizados de: `anthropic` (Python SDK, tool_use), `aiogram` (v3), `chromadb`, `pydantic`, `structlog`, `httpx`, `apscheduler`. Invocar con `use context7` o Claude lo invoca automáticamente cuando necesita docs de una librería. |

### Workflow de desarrollo

Para cada módulo o tarea:

1. **Planificar**: Leer la spec de este documento para el módulo correspondiente
2. **Consultar docs**: Usar `context7` para obtener la API actual de las librerías involucradas
3. **TDD para componentes críticos**: Usar `/test-driven-development` para safety classifier, guards, y tool execution
4. **Implementar**: Usar `/python-expert` como referencia de patterns Python
5. **Testear**: Usar `/python-testing` para unit tests, `/e2e-testing` para integration
6. **Review**: Usar `/code-reviewer` para review de calidad y `/security-review` para componentes con shell access o auth

---

## 1. Project Overview

### Qué es Emergent

Un runtime de agente autónomo personal. Single-agent, local-first, accesible vía Telegram, powered by Claude API con tool_use nativo. El agente puede ejecutar comandos en el sistema operativo del host, gestionar archivos, monitorear procesos, y mantener memoria persistente entre sesiones.

### Design Principles

1. **No-framework**: Custom agent loop. No LangChain, LangGraph, CrewAI, ni ningún agent framework. El tool_use loop de Claude es ~100 líneas de código. Un framework agrega abstracción sin valor para single-agent personal.
2. **Local-first**: SQLite (WAL mode) + ChromaDB embedded. Sin bases de datos externas.
3. **Async-everywhere**: Full asyncio. Telegram gateway es async, LLM calls son I/O bound.
4. **Observable by default**: Toda LLM call y tool execution trazada con structlog desde día 1.
5. **Security-aware**: Tier system de permisos para shell commands. Clasificación ANTES de ejecución.

### Tech Stack

| Componente | Tecnología | Notas |
|---|---|---|
| Language | Python 3.12+ | async/await, type hints, dataclasses |
| Package manager | uv | Reemplaza pip+venv |
| LLM | Claude Sonnet 4 (`claude-sonnet-4-20250514`) | tool_use nativo. Sonnet es el default; Opus para tareas complejas si se configura. |
| Telegram | aiogram v3 | Async nativo. **Consultar context7 para API actual.** |
| Persistence | SQLite 3 (stdlib) | WAL mode para concurrencia |
| Vector store | ChromaDB embedded | Sin server. Default embedding: all-MiniLM-L6-v2 |
| Scheduling | APScheduler 3.x | In-process, persistido en SQLite |
| HTTP client | httpx | Async, timeout nativo |
| Logging | structlog | JSON structured logging |
| Config | pydantic-settings 2.x | Validación de config + env vars |
| Linting | ruff | Reemplaza flake8+black+isort |
| Types | mypy (strict) | En todas las function signatures |
| Testing | pytest + pytest-asyncio | Async test support |

### Dependencias NO incluidas

No instalar: LangChain, LangGraph, CrewAI, FastAPI, Celery, Redis, PostgreSQL. Razones en Design Principles.

---

## 2. Project Structure

```
emergent/
├── pyproject.toml              # uv project config
├── CLAUDE.md                   # Claude Code project instructions
├── .env.example                # Template para secrets
├── config.yaml                 # Runtime config (model, guards, tools, system prompt)
├── Makefile                    # run, test, lint, test-e2e, dashboard
├── src/emergent/
│   ├── __init__.py
│   ├── __main__.py             # python -m emergent entrypoint
│   ├── config.py               # pydantic-settings: loads .env + config.yaml
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── runtime.py          # Agentic loop (THE core module)
│   │   ├── context.py          # Context window management + memory injection
│   │   └── prompts.py          # System prompt builder
│   ├── memory/
│   │   ├── __init__.py
│   │   ├── store.py            # SQLite operations (CRUD)
│   │   ├── summarizer.py       # Auto-summarization of long conversations
│   │   └── retriever.py        # ChromaDB semantic search
│   ├── tools/
│   │   ├── __init__.py         # create_registry() factory
│   │   ├── registry.py         # ToolDefinition, ToolRegistry, SafetyTier
│   │   ├── shell.py            # Shell exec + safety classifier
│   │   ├── files.py            # File read/write (sandboxed to $HOME)
│   │   ├── web.py              # HTTP fetch with timeout + truncation
│   │   ├── system_info.py      # CPU, RAM, disk, processes
│   │   ├── cron.py             # APScheduler cron jobs
│   │   └── memory_tools.py     # Agent-facing memory search/store
│   ├── channels/
│   │   ├── __init__.py
│   │   └── telegram.py         # aiogram v3 bot gateway
│   └── observability/
│       ├── __init__.py
│       ├── tracing.py          # TraceEvent, span context manager, JSON emitter
│       └── metrics.py          # Aggregation queries over SQLite traces
├── data/                       # Runtime data (gitignored)
│   ├── emergent.db             # SQLite database
│   └── chroma/                 # ChromaDB persistence
├── artifacts/                  # Agent output files (gitignored)
├── tests/
│   ├── conftest.py             # Shared fixtures
│   ├── test_runtime.py
│   ├── test_tools/
│   │   ├── test_registry.py
│   │   ├── test_shell.py       # CRITICAL: safety classifier tests
│   │   └── test_files.py
│   ├── test_memory/
│   │   ├── test_store.py
│   │   └── test_retriever.py
│   └── test_e2e/
│       └── test_agent_flow.py  # Real LLM calls, budget-capped
└── .gitignore                  # data/, artifacts/, .env, *.db, chroma/
```

---

## 3. CLAUDE.md (Para el proyecto)

Este archivo va en `emergent/CLAUDE.md`. Claude Code lo lee automáticamente en cada sesión:

```markdown
# Emergent — Autonomous Agent Runtime

Python 3.12+ project. Full asyncio. Package manager: uv.

## Commands
- `uv run python -m emergent` — Start agent
- `uv run pytest` — Tests
- `uv run pytest tests/test_e2e/ -k "not expensive"` — Skip costly E2E
- `uv run ruff check src/` — Lint
- `uv run mypy src/` — Type check

## Architecture
Custom agent loop using Claude API tool_use. No frameworks (no LangChain/LangGraph).
aiogram v3 for Telegram. SQLite + ChromaDB for persistence. structlog for tracing.

## Rules
- All I/O functions must be async
- Type hints on ALL function signatures
- Custom exceptions inherit from EmergentError
- Tools are ToolDefinition dataclasses registered via ToolRegistry
- Safety classifier runs BEFORE every tool execution — no exceptions
- Never hardcode API keys or tokens anywhere
- Use context7 for current API docs of all libraries before implementing

## Module dependency order
telegram.py → runtime.py → context.py + registry.py → tools/*.py + memory/*.py → tracing.py
```

---

## 4. Module Specifications

### 4.1 Agent Runtime (`src/emergent/agent/runtime.py`)

**Skills relevantes**: `/python-expert`, `/api-design`, `/backend-patterns`
**Context7**: `anthropic` Python SDK — messages.create con tool_use
**Tests**: `/test-driven-development` para guards, `/python-testing` para unit

Este es el módulo más importante. Implementa el agentic loop:

```python
class AgentRuntime:
    """
    Core agentic loop. Implements ReAct pattern using Claude's native tool_use.
    
    Loop:
        1. Build context (system prompt + memory + history + tool defs)
        2. Call Claude API
        3. If stop_reason == "tool_use": classify safety → execute/confirm/block → append result → goto 2
        4. If stop_reason == "end_turn": return text response
        5. Post-loop: persist conversation, emit traces, trigger summarization if needed
    
    Guards (hardcoded, NOT configurable by the agent):
        - max_iterations: 15
        - max_tokens_session: 100_000
        - timeout_per_tool: 30 seconds
        - timeout_session: 300 seconds (5 min)
        - max_tool_output_chars: 10_000
    """
```

**Anthropic API usage pattern** (verificar con context7 para API actual):

```python
response = await client.messages.create(
    model="claude-sonnet-4-20250514",
    system=system_prompt,        # string
    messages=messages,           # list of message dicts
    tools=tool_definitions,      # list of tool schemas
    max_tokens=4096,
)

# response.stop_reason: "end_turn" | "tool_use"
# response.content: list of ContentBlock (TextBlock | ToolUseBlock)
# response.usage: Usage(input_tokens, output_tokens)
```

**Tool call handling**:

Cuando `stop_reason == "tool_use"`, el response.content contiene ToolUseBlock(s) con:
- `id`: tool_use_id (necesario para devolver el resultado)
- `name`: nombre del tool
- `input`: dict con los parámetros

Se devuelve como:
```python
{"role": "user", "content": [{"type": "tool_result", "tool_use_id": id, "content": result_string}]}
```

**Error handling**:

```python
class EmergentError(Exception): pass
class ToolExecutionError(EmergentError): pass
class SafetyViolationError(EmergentError): pass
class ContextOverflowError(EmergentError): pass
class MaxIterationsError(EmergentError): pass
```

### 4.2 Tool System (`src/emergent/tools/`)

**Skills relevantes**: `/python-expert`, `/api-design`, `/security-review`
**Context7**: No necesario (código propio, no libs externas)
**Tests**: `/test-driven-development` para safety classifier (CRITICAL)

```python
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Any

class SafetyTier(Enum):
    TIER_1_AUTO = "auto"        # Read-only: ls, cat, ps, df, docker ps, git status
    TIER_2_CONFIRM = "confirm"  # Write/execute: kill, rm, docker restart, pip install
    TIER_3_BLOCKED = "blocked"  # Destructive: sudo, rm -rf /, curl|bash, chmod 777

@dataclass
class ToolDefinition:
    name: str
    description: str                    # Claude usa esto para decidir cuándo usar el tool
    input_schema: dict                  # JSON Schema compatible con Anthropic tool_use
    handler: Callable[..., Any]         # async function(input: dict) -> str
    safety_tier: SafetyTier             # default tier (shell overrides dynamically)
    timeout: int = 30                   # seconds
```

**Tools v1.0:**

| Tool | Default Tier | Descripción para Claude |
|---|---|---|
| `shell_execute` | DYNAMIC | Ejecuta un bash command en el host. Read-only commands (ls, cat, ps, grep) se ejecutan automáticamente. Write commands (kill, rm, mv) requieren confirmación. Destructive commands (sudo, rm -rf) están bloqueados. |
| `file_read` | TIER_1 | Lee contenido de un archivo. Path relativo a $HOME. Max 10K chars. |
| `file_write` | TIER_2 | Escribe/crea un archivo. Requiere confirmación si el archivo ya existe. Path relativo a $HOME. |
| `web_fetch` | TIER_1 | HTTP GET a una URL. Retorna text content, truncado a 10K chars. Timeout 15s. |
| `system_info` | TIER_1 | Info del sistema: CPU usage, RAM, disk space, top processes. Sin argumentos. |
| `cron_schedule` | TIER_2 | Crea/lista/elimina cron jobs. Jobs ejecutan el agent loop con un prompt predefinido. |
| `memory_search` | TIER_1 | Busca en la memoria semántica. Retorna los 3 resultados más relevantes. |
| `memory_store` | TIER_1 | Guarda un dato en long-term memory con una key descriptiva. |

**Shell safety classifier** — Implementar con `/test-driven-development`. Escribir los tests ANTES del código:

```python
# Mínimo 30 test cases cubriendo:
# - Cada comando readonly (tier 1) 
# - Cada comando destructivo conocido (tier 3)
# - Pipes: "ls | grep" (tier 1) vs "curl | bash" (tier 3)
# - Subshells: "$(rm -rf /)" debe ser tier 3
# - Semicolons: "ls; rm -rf /" debe ser tier 3
# - Redirects: "echo > /etc/passwd" debe ser tier 3
# - Variable expansion: "$HOME" en paths está ok, "$(...)" es sospechoso
```

### 4.3 Telegram Gateway (`src/emergent/channels/telegram.py`)

**Skills relevantes**: `/python-expert`, `/backend-patterns`
**Context7**: `aiogram` v3 — Router, handlers, InlineKeyboard, Bot API
**Tests**: `/python-testing`

```python
"""
aiogram v3 Telegram bot.

Responsibilities:
- Recibir mensajes de texto del usuario
- Pasar mensaje al AgentRuntime.run()
- Enviar respuesta (con chunking si > 4096 chars)
- Enviar inline keyboards para confirmaciones TIER_2
- Manejar callbacks de confirmación (approve/deny)
- Enviar typing indicator mientras el agente procesa
- Auth: whitelist de Telegram user IDs (config.yaml)

NO es responsable de:
- Lógica del agente (eso es runtime.py)
- Clasificación de seguridad (eso es registry.py)
- Persistencia (eso es memory/store.py)
"""
```

**Patrón de confirmación TIER_2:**

1. Agent loop llega a un tool TIER_2
2. Runtime suspende el loop (usa asyncio.Event o similar)
3. Telegram envía inline keyboard: `[✅ Ejecutar: kill 8432] [❌ Cancelar]`
4. Usuario apreta botón → callback handler resuelve el Event
5. Runtime continúa con el resultado (executed o cancelled)
6. Timeout de confirmación: 60 segundos → auto-cancel

### 4.4 Memory System (`src/emergent/memory/`)

**Skills relevantes**: `/python-expert`, `/backend-patterns`, `/data-analyst`
**Context7**: `chromadb` — PersistentClient, collections, embeddings
**Tests**: `/python-testing` (in-memory SQLite para aislamiento)

**SQLite schema (`store.py`):**

```sql
PRAGMA journal_mode=WAL;  -- Concurrencia: reads no bloquean writes

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
    content TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    tokens_used INTEGER,
    model TEXT
);

CREATE TABLE tool_executions (
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

CREATE TABLE traces (
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

CREATE TABLE session_summaries (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    key_topics_json TEXT,
    generated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_conversations_session ON conversations(session_id);
CREATE INDEX idx_traces_timestamp ON traces(timestamp);
CREATE INDEX idx_profile_confidence ON user_profile(confidence);
```

**ChromaDB (`retriever.py`):**

```python
# Verificar API actual con context7 antes de implementar
client = chromadb.PersistentClient(path="./data/chroma")
collection = client.get_or_create_collection(
    name="conversations",
    metadata={"hnsw:space": "cosine"}
)
# Embeds: default (all-MiniLM-L6-v2), sin API cost
# Upsert después de cada sesión
# Query: top 3 resultados por semantic similarity
```

**Context injection (`context.py`):**

Antes de cada LLM call, construir el contexto en este orden:
1. System prompt base (personalidad, reglas, capabilities)
2. User profile entries con confidence > 0.5
3. Top 3 relevant memories de ChromaDB (si existen)
4. Session summary (si la conversación actual es larga)
5. Conversation history (últimos N mensajes)
6. Buffer de 4K tokens para la respuesta

Cuando el history supera 80% del budget disponible:
1. Tomar el 70% más viejo de los mensajes
2. Hacer un LLM call de summarization
3. Reemplazar mensajes viejos con el summary
4. Guardar summary en session_summaries

### 4.5 Observability (`src/emergent/observability/`)

**Skills relevantes**: `/python-expert`, `/data-analyst`
**Context7**: `structlog`
**Tests**: `/python-testing`

```python
@dataclass
class TraceEvent:
    trace_id: str
    span_id: str
    event_type: str        # "llm_call" | "tool_exec" | "memory_op" | "error"
    timestamp: float
    duration_ms: float
    metadata: dict         # variable por event_type

# LLM call metadata:
# model, input_tokens, output_tokens, cost_usd, stop_reason, tools_requested[]

# Tool exec metadata:
# tool_name, safety_tier, user_confirmed, input_summary, output_length, error

# Emitir como JSON lines via structlog
# Persistir en traces table de SQLite
```

**Dashboard CLI (`make dashboard`):**

Query sobre SQLite traces table. Output:
- Total requests (last 24h, 7d, 30d)
- Success rate %
- Average latency (p50, p95)
- Total cost USD
- Top 5 errors by frequency
- Top 5 most expensive traces
- Tool usage distribution

---

## 5. Implementation Phases

### Phase 0: Project Setup (Día 1)

**Skills**: `/project-planner`

```bash
# 1. Init
mkdir emergent && cd emergent
uv init --name emergent --python 3.12
uv add anthropic aiogram chromadb structlog pydantic-settings httpx apscheduler
uv add --dev pytest pytest-asyncio ruff mypy

# 2. Create structure
mkdir -p src/emergent/{agent,memory,tools,channels,observability}
mkdir -p tests/{test_tools,test_memory,test_e2e}
mkdir -p data artifacts
touch src/emergent/__init__.py src/emergent/__main__.py
# ... create all __init__.py files

# 3. Create CLAUDE.md (section 3 de este documento)
# 4. Create .env.example, config.yaml, Makefile, .gitignore
# 5. git init && initial commit
```

**Acceptance**: `uv run python -c "import emergent"` funciona. `uv run ruff check src/` pasa.

### Phase 1: Minimal Agent Loop (Días 2-5)

**Skills**: `/python-expert`, `/api-design`, `/backend-patterns`
**Context7**: `anthropic` SDK (messages.create, tool_use), `aiogram` v3

Implementar en este orden:
1. `config.py` — Cargar .env y config.yaml con pydantic-settings
2. `agent/prompts.py` — System prompt hardcoded inicial
3. `agent/runtime.py` — Agent loop SIN tools (solo conversación)
4. `channels/telegram.py` — Bot básico: recibe mensaje → runtime.run() → responde
5. `__main__.py` — Entrypoint que conecta todo

**Acceptance**: Mandar un mensaje por Telegram → Claude responde coherentemente. Multi-turn funciona.

### Phase 2: Tool System (Días 6-10)

**Skills**: `/test-driven-development` (safety classifier), `/python-expert`, `/security-review`
**Context7**: No requerido (código propio)

Implementar en este orden:
1. `tools/registry.py` — ToolDefinition, ToolRegistry, SafetyTier
2. **Tests del safety classifier PRIMERO** (TDD — escribir tests antes del código)
3. `tools/shell.py` — Shell execute + safety classifier
4. `tools/files.py` — File read/write
5. `tools/system_info.py` — System info
6. `tools/web.py` — HTTP fetch
7. Integrar tool_definitions en el agent loop
8. Telegram inline keyboards para confirmaciones TIER_2
9. `/code-reviewer` sobre todo el tool system
10. `/security-review` sobre shell.py y registry.py

**Acceptance**: "Mostrá los containers corriendo" funciona E2E. "Kill PID X" pide confirmación.

### Phase 3: Persistence & Memory (Días 11-16)

**Skills**: `/python-expert`, `/backend-patterns`, `/data-analyst`
**Context7**: `chromadb` (PersistentClient API)

1. `memory/store.py` — SQLite schema + CRUD operations
2. `memory/retriever.py` — ChromaDB setup + semantic search
3. `memory/summarizer.py` — Auto-summarization con LLM call
4. `agent/context.py` — Context injection pipeline
5. User profile auto-construction
6. Context window truncation strategy
7. Tests: persistence across restart, semantic search accuracy

**Acceptance**: Reiniciar servicio → "qué hablamos ayer?" → responde con contexto.

### Phase 4: Observability (Días 17-20)

**Skills**: `/python-expert`, `/data-analyst`
**Context7**: `structlog`

1. `observability/tracing.py` — TraceEvent + structlog JSON emitter
2. Decorators/context managers para spans
3. Integrar traces en runtime.py (cada LLM call y tool exec)
4. `observability/metrics.py` — SQLite queries de agregación
5. Makefile target `dashboard`

**Acceptance**: `make dashboard` muestra métricas reales después de 1 día de uso.

### Phase 5: Cron & Proactive Agent (Días 21-25)

**Skills**: `/python-expert`, `/backend-patterns`
**Context7**: `apscheduler`

1. `tools/cron.py` — APScheduler + SQLite persistence
2. `tools/memory_tools.py` — Agent-facing memory operations
3. Proactive notifications vía Telegram
4. Built-in jobs: system health check, daily summary

**Acceptance**: "Avisame si Docker se cae" → crea cron → notifica si detecta container down.

### Phase 6: Hardening (Días 26-30)

**Skills**: `/deployment-patterns`, `/security-review`, `/e2e-testing`, `/docker-patterns`

1. Error recovery (retry con backoff en Claude API)
2. Graceful shutdown (SIGTERM)
3. Systemd service file
4. Log rotation
5. `/e2e-testing` — Suite E2E completa con LLM calls reales (budget cap)
6. `/security-review` — Review final de todo el sistema
7. `/code-reviewer` — Review final
8. 7 días de uso real → colectar issues → fix

**Acceptance**: 7 días running sin crashes. Success criteria v1.0 cumplidos.

---

## 6. Success Criteria (v1.0)

- [ ] Conversación funcional vía Telegram con Claude tool_use
- [ ] Al menos 5 tools operativos (shell, files, web, system_info, memory)
- [ ] Safety classifier con 100% de cobertura en tests
- [ ] Memoria persistente cross-session
- [ ] Trace completo de cada interacción (latency, cost, tools, success)
- [ ] Dashboard de métricas funcional
- [ ] 7 días corriendo sin crashes ni memory leaks
- [ ] Cost promedio < $0.05 por request

---

## 7. Risks & Mitigations

| Riesgo | Impacto | Mitigación |
|---|---|---|
| Context overflow en conversaciones largas | Agent pierde contexto o falla | Summarization automática + truncation. Implementar en Phase 3, no después. |
| Cost runaway (loop infinito) | API key agotada | Guards hardcoded: max_iterations=15, max_tokens=100K. No configurables por el agente. |
| Shell injection | Ejecución de comandos peligrosos | Safety classifier (TDD) + blocked patterns + confirmation. Usar `/security-review`. |
| ChromaDB corruption | Pérdida de memoria semántica | SQLite es source of truth (L0). ChromaDB es derivado, se puede reconstruir. |
| Telegram rate limits | Mensajes largos fallan | Chunking automático (4096 chars). Typing indicator. |
| aiogram breaking changes | Bot deja de funcionar | Pinear versión en pyproject.toml. Verificar con context7 antes de implementar. |

---

*Este documento es la single source of truth para Emergent.*
*Cada módulo referencia las skills y herramientas específicas a usar.*
*Actualizar este documento con cada decisión de diseño tomada durante el build.*
