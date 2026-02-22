# Research Worker Design

**Fecha:** 2026-02-22
**Estado:** Aprobado para implementar
**Autor:** Claude + Nacho

---

## Objetivo

Worker autonomo de investigacion que corra diariamente a las 9:30 AM via APScheduler,
busque en fuentes especializadas lo mas relevante para mejorar Emergent, puntue y
rankee findings con heuristicas deterministas, persista todo estructuradamente en
SQLite + ChromaDB, genere reportes markdown, y entregue un digest con highlights
separados por Telegram.

---

## Arquitectura

```
APScheduler (9:30 AM daily)
    |
    v
ResearchWorker.run()
    |
    +---> ArxivSource      ----------+
    +---> GitHubSource     ----------+
    +---> HackerNewsSource ----------+  asyncio.gather (paralelo)
    +---> RedditSource     ----------+
    +---> RSSSource        ----------+
    +---> TavilySource     ----------+
                                     |
                                     v
                           Deduplicate (by URL)
                                     |
                                     v
                           Score + Rank (deterministic)
                                     |
                                     v
                    +----------------+----------------+----------------+
                    v                v                v                v
              SQLite           ChromaDB          Markdown         Telegram
        (research_findings) (research col)  (data/research/)     (digest)
```

### Dependency Direction

```
__main__.py -> research/worker.py -> research/sources.py (uses tools/research.py)
                                  -> research/scoring.py
                                  -> research/formatter.py
                                  -> memory/store.py
                                  -> memory/retriever.py
                                  -> observability/tracing.py
```

Respeta la direccion existente del proyecto: `research/` consume `tools/`, `memory/`,
`observability/` -- nunca al reves.

---

## Componentes

### 1. Source Fetchers — `src/emergent/tools/research.py`

Cada source es una funcion async independiente. Todas retornan `list[ResearchFinding]`
(dataclass normalizado). Se registran como tools TIER_1_AUTO para uso interactivo
on-demand ademas de ser consumidas por el worker.

| Funcion | Fuente | API | Auth | Rate Limit |
|---|---|---|---|---|
| `arxiv_search(query, max_results, categories)` | ArXiv | `export.arxiv.org/api/query` (Atom XML) | Ninguna | Semaforo 1 req/3s |
| `github_search(query, sort, max_results)` | GitHub | `api.github.com/search/repositories` | PAT via env | 30 search/min |
| `github_releases(repos)` | GitHub | `api.github.com/repos/{}/releases` | PAT via env | 5000/hr |
| `hackernews_search(query, max_results)` | HN Algolia | `hn.algolia.com/api/v1/search` | Ninguna | Generoso |
| `reddit_search(query, subreddits, max_results)` | Reddit | `.json` endpoints | User-Agent | ~10 req/min |
| `rss_fetch(feed_urls)` | RSS | URLs directas | Ninguna | N/A |
| `tavily_search(query, max_results, topic, time_range)` | Tavily | `api.tavily.com` | API key via env | 1000 free/mes |

#### Dataclass normalizado

```python
@dataclass
class ResearchFinding:
    source: str           # "arxiv", "github", "hn", "reddit", "rss", "tavily"
    title: str
    url: str
    summary: str          # abstract, description, o snippet
    published_at: datetime | None
    metadata: dict        # stars, upvotes, authors, category, etc.
```

#### Decisiones de implementacion

- Todo con `httpx.AsyncClient` (ya es dependencia del proyecto).
- ArXiv XML se parsea con `feedparser` (nueva dep, ~100KB, pura).
- RSS tambien usa `feedparser`.
- Tavily usa `AsyncTavilyClient` del SDK `tavily-python` (nueva dep, ~50KB) -- tiene
  cliente async nativo que encaja en el codebase async-first.
- SSRF prevention: reutiliza `_check_ssrf()` de `web.py` para cualquier URL construida
  dinamicamente.
- Cada source tiene `try/except` individual -- si una falla, las demas continuan y se
  logea un warning via structlog.
- Timeout de 15s por request, 1 retry en 5xx (mismo patron que `web_fetch`).
- Semaforo por fuente para rate limiting (e.g., ArXiv: `asyncio.Semaphore(1)` + sleep 3s).

---

### 2. Research Domains — `src/emergent/research/sources.py`

Configuracion de queries optimizadas por dominio. Todo enfocado en mejorar Emergent.

```python
RESEARCH_DOMAINS = {
    "agent_architecture": {
        "description": "Multi-agent orchestration, tool use, ReAct patterns, agent loops",
        "arxiv_queries": [
            "autonomous LLM agents",
            "tool use language models",
            "multi-agent orchestration",
        ],
        "arxiv_categories": ["cs.AI", "cs.CL", "cs.SE"],
        "github_queries": [
            "agent framework language:python stars:>50 pushed:>{7d}",
        ],
        "hn_queries": ["AI agent", "autonomous agent", "tool use LLM"],
        "subreddits": ["MachineLearning", "LocalLLaMA"],
        "tavily_queries": ["LLM agent architecture patterns 2026"],
    },
    "rag_memory": {
        "description": "RAG, embeddings, vector stores, memory architectures for agents",
        "arxiv_queries": [
            "retrieval augmented generation",
            "semantic memory agents",
        ],
        "arxiv_categories": ["cs.IR", "cs.CL"],
        "github_queries": [
            "RAG framework language:python stars:>30 pushed:>{7d}",
        ],
        "hn_queries": ["RAG", "vector database", "embeddings"],
        "subreddits": ["MachineLearning"],
        "tavily_queries": ["RAG optimization techniques 2026"],
    },
    "python_ecosystem": {
        "description": "Python libraries, async patterns, performance, new releases",
        "arxiv_queries": [],
        "github_queries": [
            "language:python stars:>100 pushed:>{7d} topic:async",
        ],
        "hn_queries": ["Python", "asyncio", "uv package manager"],
        "subreddits": ["Python"],
        "tavily_queries": ["Python asyncio best practices 2026"],
        "releases_repos": [
            "astral-sh/uv",
            "pydantic/pydantic",
        ],
    },
    "security_safety": {
        "description": "Agent safety, prompt injection, LLM security, guardrails",
        "arxiv_queries": [
            "prompt injection defense",
            "LLM safety guardrails",
        ],
        "arxiv_categories": ["cs.CR", "cs.AI"],
        "github_queries": [
            "LLM security language:python stars:>20 pushed:>{7d}",
        ],
        "hn_queries": ["prompt injection", "AI safety", "LLM security"],
        "subreddits": ["netsec", "MachineLearning"],
        "tavily_queries": ["LLM agent security best practices 2026"],
    },
    "devops_infra": {
        "description": "Docker, deployment, monitoring, observability for agent systems",
        "arxiv_queries": [],
        "github_queries": [
            "agent deployment docker stars:>20 pushed:>{7d}",
        ],
        "hn_queries": ["agent deployment", "LLM monitoring"],
        "subreddits": ["devops", "selfhosted"],
        "tavily_queries": ["autonomous agent deployment monitoring 2026"],
    },
    "emergent_deps": {
        "description": "Releases and changes in Emergent's direct dependencies",
        "releases_repos": [
            "anthropics/anthropic-sdk-python",
            "aiogram/aiogram",
            "chroma-core/chroma",
            "agronholm/apscheduler",
            "hynek/structlog",
            "astral-sh/uv",
            "encode/httpx",
        ],
    },
}
```

#### RSS Feeds

```python
RSS_FEEDS = [
    {"url": "https://www.anthropic.com/feed.xml", "domain": "agent_architecture"},
    {"url": "https://simonwillison.net/atom/everything/", "domain": "agent_architecture"},
    {"url": "https://openai.com/blog/rss.xml", "domain": "agent_architecture"},
    {"url": "https://blog.langchain.dev/rss/", "domain": "rag_memory"},
]
```

Los dominios y feeds se pueden overridear/extender desde `config.yaml`.

---

### 3. Scoring System — `src/emergent/research/scoring.py`

Sistema de scoring determinista (NO LLM). Cumple AGENTS.md: clasificadores deben ser
deterministic pattern matching.

```python
def score_finding(finding: ResearchFinding, domain_config: dict) -> float:
    """Score 0.0-1.0, deterministic, no LLM calls."""
    score = 0.0

    # 1. Recency (40% weight)
    # Linear decay over 30 days. Mas reciente = mas relevante.
    age_days = (now - finding.published_at).days if finding.published_at else 15
    recency = max(0.0, 1.0 - (age_days / 30))
    score += 0.40 * recency

    # 2. Engagement (25% weight)
    # Normalizado por fuente: stars para github, points para HN, upvotes para reddit.
    engagement = _normalize_engagement(finding.source, finding.metadata)
    score += 0.25 * engagement

    # 3. Keyword relevance (25% weight)
    # Match de keywords del dominio contra title + summary.
    # Usa sets de terms por dominio, cuenta interseccion normalizada.
    relevance = _keyword_match_score(
        text=f"{finding.title} {finding.summary}",
        domain_keywords=domain_config.get("keywords", []),
    )
    score += 0.25 * relevance

    # 4. Source authority (10% weight)
    # Peso fijo por fuente.
    authority = SOURCE_AUTHORITY.get(finding.source, 0.5)
    score += 0.10 * authority

    return round(score, 4)
```

#### Source Authority Weights

```python
SOURCE_AUTHORITY = {
    "arxiv": 1.0,     # peer-reviewed papers
    "github": 0.9,    # code + stars = signal real
    "tavily": 0.7,    # web search, quality varies
    "hn": 0.7,        # curated by community
    "rss": 0.6,       # depends on feed source
    "reddit": 0.5,    # high noise ratio
}
```

#### Engagement Normalization

```python
def _normalize_engagement(source: str, metadata: dict) -> float:
    """Normalize engagement metrics to 0.0-1.0 per source."""
    if source == "github":
        stars = metadata.get("stars", 0)
        return min(1.0, stars / 1000)  # 1000+ stars = max
    elif source == "hn":
        points = metadata.get("points", 0)
        return min(1.0, points / 500)  # 500+ points = max
    elif source == "reddit":
        score = metadata.get("score", 0)
        return min(1.0, score / 1000)
    elif source == "arxiv":
        # ArXiv no tiene engagement metrics directos
        return 0.5  # neutral
    else:
        return 0.3  # unknown source, conservative
```

#### Highlight Separation

```python
HIGHLIGHT_THRESHOLD = 0.7  # configurable via config.yaml
MAX_HIGHLIGHTS = 5

def separate_highlights(
    findings: list[ScoredFinding],
    threshold: float = HIGHLIGHT_THRESHOLD,
    max_highlights: int = MAX_HIGHLIGHTS,
) -> tuple[list[ScoredFinding], list[ScoredFinding]]:
    """Separate top findings from the rest."""
    sorted_findings = sorted(findings, key=lambda f: f.score, reverse=True)
    highlights = [f for f in sorted_findings if f.score >= threshold][:max_highlights]
    rest = [f for f in sorted_findings if f not in highlights and f.score >= 0.3]
    return highlights, rest
```

---

### 4. Formatter — `src/emergent/research/formatter.py`

Genera dos formatos de output.

#### Markdown Report (`data/research/YYYY-MM-DD.md`)

```markdown
# Research Digest -- 2026-02-23

## Highlights

### [1] Multi-Agent Tool Orchestration with Safety Guarantees
- **Score:** 0.92 | **Source:** arxiv | **Published:** 2026-02-20
- **URL:** https://arxiv.org/abs/2602.12345
- **Summary:** Novel approach to safe tool execution in multi-agent systems
  using formal verification of tool call chains...
- **Domain:** agent_architecture

### [2] ChromaDB 2.0 Release
- **Score:** 0.87 | **Source:** github | **Published:** 2026-02-22
- **URL:** https://github.com/chroma-core/chroma/releases/tag/v2.0.0
- **Summary:** Major release with native async support, improved HNSW index...
- **Domain:** emergent_deps

## All Findings by Domain

### agent_architecture (5 findings)
| Score | Title | Source | URL |
|---|---|---|---|
| 0.92 | Multi-Agent Tool Orchestration... | arxiv | [link](...) |
| 0.71 | New ReAct Pattern Variant... | hn | [link](...) |
| 0.65 | Agent Memory Architecture Survey | arxiv | [link](...) |
| 0.52 | Tool Use in Production Agents | tavily | [link](...) |
| 0.41 | Simple Agent Framework | github | [link](...) |

### rag_memory (4 findings)
...

### emergent_deps (3 findings)
...

---
Run ID: abc123 | Sources queried: 6 | Total findings: 47 | Highlights: 5
Generated by Emergent Research Worker -- 2026-02-23 09:31:42
```

#### Telegram Digest (condensado, max 4096 chars)

```
Research Digest -- Feb 23

TOP FINDINGS:

1. Multi-Agent Tool Orchestration (ArXiv, 0.92)
   arxiv.org/abs/2602.12345

2. ChromaDB 2.0 Released (GitHub, 0.87)
   github.com/chroma-core/chroma/releases/tag/v2.0.0

3. Prompt Injection Defense via Input Sanitization (ArXiv, 0.81)
   arxiv.org/abs/2602.67890

+39 more findings in data/research/2026-02-23.md
```

---

### 5. SQLite Schema — `research_findings` table

Agregada al schema existente de `src/emergent/memory/store.py`.

```sql
CREATE TABLE IF NOT EXISTS research_findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT,
    relevance_score REAL,
    is_highlight BOOLEAN DEFAULT 0,
    published_at DATETIME,
    metadata_json TEXT,
    found_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(url)
);

CREATE INDEX IF NOT EXISTS idx_findings_domain
    ON research_findings(domain);
CREATE INDEX IF NOT EXISTS idx_findings_score
    ON research_findings(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_findings_found
    ON research_findings(found_at);
CREATE INDEX IF NOT EXISTS idx_findings_highlight
    ON research_findings(is_highlight) WHERE is_highlight = 1;
CREATE INDEX IF NOT EXISTS idx_findings_run
    ON research_findings(run_id);
```

#### Nuevos metodos en MemoryStore

```python
async def save_research_findings(self, findings: list[dict]) -> int:
    """Bulk insert findings. Returns count of new insertions (ignores duplicates by URL)."""

async def get_research_highlights(self, days: int = 7) -> list[dict]:
    """Get highlight findings from the last N days."""

async def get_research_by_domain(self, domain: str, limit: int = 20) -> list[dict]:
    """Get findings for a specific domain, ordered by score."""

async def search_research(self, query: str, limit: int = 10) -> list[dict]:
    """LIKE search in title + summary."""

async def cleanup_old_research(self, ttl_days: int = 90) -> None:
    """Delete findings older than TTL. Integrated into existing cleanup_old_data()."""
```

---

### 6. ChromaDB — Nueva coleccion `research`

Los highlights se indexan en ChromaDB en una coleccion separada `"research"` (la
existente `"conversations"` no se toca). Esto permite que el agente busque en
findings via `memory_search`.

Cambio en `SemanticRetriever`:

```python
# Nueva coleccion
self._research_collection = self._client.get_or_create_collection(
    name="research",
    metadata={"hnsw:space": "cosine"},
)

async def upsert_research_findings(self, findings: list[ResearchFinding]) -> None:
    """Index research findings for semantic search."""

async def search_research(self, query: str, n_results: int = 5) -> list[dict]:
    """Search research findings collection."""
```

---

### 7. Worker Orchestrator — `src/emergent/research/worker.py`

Clase principal que orquesta todo el pipeline.

```python
class ResearchWorker:
    def __init__(
        self,
        store: MemoryStore,
        retriever: SemanticRetriever,
        settings: EmergentSettings,
        telegram_notify: TelegramGateway | None = None,
    ) -> None: ...

    async def run(self) -> None:
        """Main entry point. Called by APScheduler daily at 9:30 AM."""
        run_id = str(uuid.uuid4())[:8]
        log.info("research_run_start", run_id=run_id)

        try:
            # 1. Fetch from all sources in parallel
            raw_findings = await self._fetch_all_sources()

            # 2. Deduplicate by URL
            unique_findings = self._deduplicate(raw_findings)

            # 3. Score and rank
            scored = self._score_all(unique_findings)

            # 4. Separate highlights
            highlights, rest = separate_highlights(scored)

            # 5. Persist to SQLite
            count = await self.store.save_research_findings(
                self._to_dicts(scored, highlights, run_id)
            )

            # 6. Index highlights in ChromaDB
            await self.retriever.upsert_research_findings(highlights)

            # 7. Write markdown report
            report_path = self._write_markdown(highlights, rest, run_id)

            # 8. Send Telegram digest
            if self.telegram_notify:
                await self._send_telegram_digest(highlights, rest, report_path)

            log.info(
                "research_run_done",
                run_id=run_id,
                total_findings=len(scored),
                highlights=len(highlights),
                new_persisted=count,
            )

        except Exception as e:
            log.error("research_run_failed", run_id=run_id, error=str(e))
            # Never crash the scheduler -- log and continue

    async def _fetch_all_sources(self) -> list[ResearchFinding]:
        """Fetch from all configured sources in parallel."""
        tasks = []
        for domain_name, domain_config in RESEARCH_DOMAINS.items():
            tasks.extend(self._build_tasks_for_domain(domain_name, domain_config))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        findings = []
        for result in results:
            if isinstance(result, Exception):
                log.warning("source_fetch_failed", error=str(result))
            else:
                findings.extend(result)
        return findings

    async def run_adhoc(self, topic: str) -> tuple[list, list]:
        """Run ad-hoc research on a specific topic. Used by research_run tool."""
        # Similar to run() but with custom queries derived from topic
        ...
```

---

### 8. Interactive Tools — On-demand research

Ademas del job automatico, se registran 2 tools para uso en conversacion.

| Tool | Safety Tier | Descripcion |
|---|---|---|
| `research_search` | TIER_1_AUTO | Buscar en findings guardados (SQLite full-text + ChromaDB semantic) |
| `research_run` | TIER_2_CONFIRM | Disparar investigacion ad-hoc sobre un tema especifico |

#### research_search

```python
RESEARCH_SEARCH_DEFINITION = {
    "name": "research_search",
    "description": (
        "Search through stored research findings. Searches by keyword in titles "
        "and summaries, optionally filtered by domain. Returns scored results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "domain": {
                "type": "string",
                "description": "Optional domain filter",
                "enum": [
                    "agent_architecture", "rag_memory", "python_ecosystem",
                    "security_safety", "devops_infra", "emergent_deps",
                ],
            },
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
}
```

#### research_run

```python
RESEARCH_RUN_DEFINITION = {
    "name": "research_run",
    "description": (
        "Run an ad-hoc research investigation on a specific topic. "
        "Searches across all configured sources (ArXiv, GitHub, HN, Reddit, RSS, Tavily). "
        "Results are scored, persisted, and returned. Use for on-demand deep dives."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Topic to research (e.g., 'WebSocket patterns for agents')",
                "maxLength": 200,
            },
            "max_results": {"type": "integer", "default": 10, "maximum": 20},
        },
        "required": ["topic"],
    },
}
```

---

### 9. Wiring — `src/emergent/__main__.py`

```python
# After initializing store, retriever, and gateway:

from emergent.research.worker import ResearchWorker

research_cfg = settings.research or {}
if research_cfg.get("enabled", True):
    research_worker = ResearchWorker(
        store=store,
        retriever=retriever,
        settings=settings,
        telegram_notify=gateway,
    )

    # Daily research job at 9:30 AM
    scheduler.add_job(
        research_worker.run,
        trigger="cron",
        hour=9,
        minute=30,
        id="research_daily",
        name="daily_research",
        replace_existing=True,
    )

    # Register interactive research tools
    registry.register(
        ToolDefinition(
            name="research_search",
            description=RESEARCH_SEARCH_DEFINITION["description"],
            input_schema=RESEARCH_SEARCH_DEFINITION["input_schema"],
            handler=make_research_search_handler(store, retriever),
            safety_tier=SafetyTier.TIER_1_AUTO,
        )
    )
    registry.register(
        ToolDefinition(
            name="research_run",
            description=RESEARCH_RUN_DEFINITION["description"],
            input_schema=RESEARCH_RUN_DEFINITION["input_schema"],
            handler=make_research_run_handler(research_worker),
            safety_tier=SafetyTier.TIER_2_CONFIRM,
        )
    )
```

---

### 10. Configuration

#### config.yaml (nueva seccion)

```yaml
research:
  enabled: true
  schedule: "30 9 * * *"
  max_findings_per_domain: 10
  highlight_threshold: 0.7
  max_highlights: 5
  web_search_provider: "tavily"
```

#### .env.example (nuevas vars)

```bash
# Research (optional -- worker degrades gracefully without them)
TAVILY_API_KEY=tvly-...
GITHUB_TOKEN=ghp_...
```

#### config.py (nuevo dataclass)

```python
@dataclass
class ResearchConfig:
    enabled: bool = True
    schedule: str = "30 9 * * *"
    max_findings_per_domain: int = 10
    highlight_threshold: float = 0.7
    max_highlights: int = 5
    web_search_provider: str = "tavily"
```

---

## Nuevas Dependencias

| Package | Version | Motivo | Tamano |
|---|---|---|---|
| `feedparser` | `>=6.0` | Parseo RSS/Atom (ArXiv XML + RSS feeds) | ~100KB |
| `tavily-python` | `>=0.5.0` | AsyncTavilyClient para web search general | ~50KB |

Ambas se agregan en `pyproject.toml` bajo `dependencies`.

---

## Graceful Degradation

El worker nunca debe crashear ni bloquear el scheduler. Estrategia:

| Situacion | Comportamiento |
|---|---|
| TAVILY_API_KEY no configurada | Skip Tavily source, log warning, continuar con el resto |
| GITHUB_TOKEN no configurado | Usar GitHub API sin auth (60 req/hr, suficiente para daily) |
| Una fuente devuelve error/timeout | Log warning, continuar con las demas fuentes |
| Todas las fuentes fallan | Log error, no generar reporte, no enviar Telegram |
| ChromaDB no disponible | Persist en SQLite, skip indexing, log warning |
| Telegram gateway no configurado | Solo persistir + markdown, no enviar digest |
| data/research/ no existe | Crearlo automaticamente |

---

## Archivos a Crear

| Archivo | Descripcion |
|---|---|
| `src/emergent/research/__init__.py` | Exports del modulo |
| `src/emergent/research/worker.py` | Orquestador principal (ResearchWorker) |
| `src/emergent/research/sources.py` | RESEARCH_DOMAINS, RSS_FEEDS, domain configs |
| `src/emergent/research/scoring.py` | Score heuristico, highlight separation |
| `src/emergent/research/formatter.py` | Markdown + Telegram digest generators |
| `src/emergent/tools/research.py` | 7 source fetchers + tool definitions |
| `tests/test_research/__init__.py` | Test module |
| `tests/test_research/test_scoring.py` | Tests para scoring determinista |
| `tests/test_research/test_sources.py` | Tests para source fetchers (mocked HTTP) |
| `tests/test_research/test_formatter.py` | Tests para output generation |
| `tests/test_research/test_worker.py` | Tests para el orchestrator |

## Archivos a Modificar

| Archivo | Cambio |
|---|---|
| `src/emergent/memory/store.py` | Tabla `research_findings` + CRUD methods |
| `src/emergent/memory/retriever.py` | Coleccion `research` + upsert/search methods |
| `src/emergent/__main__.py` | Wiring del ResearchWorker + APScheduler job + tool registration |
| `src/emergent/tools/__init__.py` | Registrar research tools en create_registry() |
| `src/emergent/config.py` | ResearchConfig dataclass + carga desde yaml/env |
| `config.yaml` | Seccion `research:` |
| `pyproject.toml` | Agregar feedparser, tavily-python |
| `.env.example` | TAVILY_API_KEY, GITHUB_TOKEN |

---

## Orden de Implementacion

1. **Dependencies**: `pyproject.toml` + `uv lock`
2. **Source fetchers**: `tools/research.py` -- son independientes, testeable aislado
3. **SQLite schema + CRUD**: `memory/store.py` -- tabla + metodos
4. **ChromaDB extension**: `memory/retriever.py` -- coleccion research
5. **Scoring system**: `research/scoring.py` -- determinista, unit testeable
6. **Sources config**: `research/sources.py` -- domains, feeds, queries
7. **Formatter**: `research/formatter.py` -- markdown + telegram
8. **Worker orchestrator**: `research/worker.py` -- une todo
9. **Config**: `config.py` + `config.yaml` + `.env.example`
10. **Wiring**: `__main__.py` + `tools/__init__.py`
11. **Tests**: unit tests para scoring, formatters, mocked sources
12. **Integration test**: run completo con sources mockeadas

---

## Testing Strategy

### Unit Tests

- `test_scoring.py`: scoring con datos sinteticos, edge cases (missing dates, zero
  engagement, unknown sources), highlight separation logic.
- `test_formatter.py`: markdown generation, telegram digest truncation, edge cases
  (0 findings, all highlights, no highlights).
- `test_sources.py`: cada source fetcher con HTTP responses mockeadas via
  `httpx.MockTransport`. Verifica parsing correcto de ArXiv XML, GitHub JSON,
  HN JSON, Reddit JSON, RSS XML.

### Integration Tests

- `test_worker.py`: worker completo con todas las sources mockeadas, verifica:
  - Findings se persisten en SQLite
  - Highlights se indexan en ChromaDB
  - Markdown report se genera correctamente
  - Graceful degradation cuando una source falla

### What NOT to Test

- No test E2E contra APIs reales (rate limits, flaky).
- No test del wiring de APScheduler (ya testeado por la lib).

---

## API Reference

### ArXiv

- Base URL: `http://export.arxiv.org/api/query`
- Params: `search_query`, `start`, `max_results`, `sortBy`, `sortOrder`
- Query syntax: `cat:cs.AI AND all:agent` (category + keyword)
- Response: Atom 1.0 XML, parseable con feedparser
- Rate: 1 request per 3 seconds recommended
- Auth: none
- Docs: https://info.arxiv.org/help/api/

### GitHub Search

- Base URL: `https://api.github.com/search/repositories`
- Params: `q`, `sort`, `order`, `per_page`
- Query syntax: `agent framework language:python stars:>50 pushed:>2026-02-15`
- Response: JSON (`items[]` with `full_name`, `description`, `stargazers_count`, `html_url`)
- Auth: `Authorization: Bearer {GITHUB_TOKEN}` (optional but recommended)
- Rate: 10 search/min unauth, 30/min auth

### GitHub Releases

- URL: `https://api.github.com/repos/{owner}/{repo}/releases?per_page=5`
- Response: JSON array with `tag_name`, `name`, `body`, `published_at`, `html_url`

### HN Algolia

- Base URL: `https://hn.algolia.com/api/v1/search`
- Params: `query`, `tags` (story/comment), `numericFilters` (points, num_comments), `hitsPerPage`
- Response: JSON (`hits[]` with `title`, `url`, `points`, `num_comments`, `created_at`)
- Auth: none
- Rate: generous, undisclosed

### Reddit

- URL pattern: `https://www.reddit.com/r/{subreddit}/search.json`
- Params: `q`, `restrict_sr=1`, `sort=relevance`, `t=week`, `limit=10`
- Headers: `User-Agent: Emergent-Agent/0.1` (required to avoid 429)
- Response: JSON (`data.children[]` with `data.title`, `data.url`, `data.score`, `data.permalink`)
- Auth: none for .json endpoints

### Tavily

- Base URL: `https://api.tavily.com/search` (via AsyncTavilyClient)
- Key params: `query`, `search_depth` (basic/advanced), `topic`, `max_results`, `time_range`
- Response: JSON with `results[]` having `title`, `url`, `content`, `score`
- Auth: `TAVILY_API_KEY` env var
- Free tier: 1000 credits/month, no credit card required

### RSS

- URLs: configurable per domain
- Parsed via feedparser from fetched XML
- Fields: `entries[].title`, `entries[].link`, `entries[].summary`, `entries[].published`
