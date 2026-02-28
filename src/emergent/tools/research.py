"""Research source fetchers and interactive research tool handlers."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from emergent.memory.retriever import SemanticRetriever
from emergent.memory.store import MemoryStore
from emergent.research.types import ResearchFinding
from emergent.tools.registry import ToolDefinitionDict
from emergent.tools.web import _check_ssrf

logger = structlog.get_logger(__name__)

_REQUEST_TIMEOUT_SECONDS = 15
_MAX_RETRIES = 1
_ARXIV_MIN_INTERVAL_SECONDS = 3.0
_ARXIV_LOCK = asyncio.Lock()
_arxiv_last_call_monotonic = 0.0


async def _request_text(url: str, headers: dict[str, str] | None = None) -> str:
    _check_ssrf(url)
    retries = 0
    while True:
        try:
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_SECONDS, follow_redirects=True
            ) as client:
                response = await client.get(url, headers=headers)
            if response.status_code >= 500 and retries < _MAX_RETRIES:
                retries += 1
                continue
            response.raise_for_status()
            return response.text
        except (httpx.TimeoutException, httpx.HTTPStatusError):
            if retries < _MAX_RETRIES:
                retries += 1
                continue
            raise


async def _request_json(url: str, headers: dict[str, str] | None = None) -> Any:
    payload = await _request_text(url, headers=headers)
    return json.loads(payload)


def _to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(UTC)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        return None


async def arxiv_search(
    query: str,
    max_results: int = 10,
    categories: list[str] | None = None,
    *,
    domain: str,
) -> list[ResearchFinding]:
    """Search ArXiv Atom API and return normalized findings."""
    async with _ARXIV_LOCK:
        global _arxiv_last_call_monotonic
        now = time.monotonic()
        elapsed = now - _arxiv_last_call_monotonic
        if elapsed < _ARXIV_MIN_INTERVAL_SECONDS:
            await asyncio.sleep(_ARXIV_MIN_INTERVAL_SECONDS - elapsed)
        _arxiv_last_call_monotonic = time.monotonic()

    parts = [f"all:{query}"]
    if categories:
        category_query = " OR ".join([f"cat:{category}" for category in categories])
        parts.append(f"({category_query})")
    search_query = " AND ".join(parts)

    url = "https://export.arxiv.org/api/query?" + urlencode(
        {
            "search_query": search_query,
            "start": 0,
            "max_results": max(1, min(max_results, 20)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
    )

    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser_missing", source="arxiv")
        return []

    xml_payload = await _request_text(url)
    feed = feedparser.parse(xml_payload)

    findings: list[ResearchFinding] = []
    for entry in feed.entries:
        findings.append(
            ResearchFinding(
                source="arxiv",
                domain=domain,
                title=str(getattr(entry, "title", ""))[:300],
                url=str(getattr(entry, "link", "")),
                summary=str(getattr(entry, "summary", ""))[:600],
                published_at=_to_datetime(getattr(entry, "published", None)),
                metadata={"id": str(getattr(entry, "id", ""))},
            )
        )
    return findings


def _github_headers() -> dict[str, str]:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Emergent-Agent/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def github_search(
    query: str,
    sort: str = "stars",
    max_results: int = 10,
    *,
    domain: str,
) -> list[ResearchFinding]:
    """Search GitHub repositories and return normalized findings."""
    url = "https://api.github.com/search/repositories?" + urlencode(
        {
            "q": query,
            "sort": sort,
            "order": "desc",
            "per_page": max(1, min(max_results, 20)),
        }
    )
    payload = await _request_json(url, headers=_github_headers())
    findings: list[ResearchFinding] = []
    for item in payload.get("items", []):
        findings.append(
            ResearchFinding(
                source="github",
                domain=domain,
                title=str(item.get("full_name", "")),
                url=str(item.get("html_url", "")),
                summary=str(item.get("description", ""))[:600],
                published_at=_to_datetime(item.get("pushed_at")),
                metadata={
                    "stars": int(item.get("stargazers_count", 0)),
                    "language": str(item.get("language", "")),
                },
            )
        )
    return findings


async def github_releases(repos: list[str], *, domain: str) -> list[ResearchFinding]:
    """Fetch latest releases for a list of repos."""
    findings: list[ResearchFinding] = []
    headers = _github_headers()
    for repo in repos[:20]:
        url = f"https://api.github.com/repos/{repo}/releases?per_page=3"
        try:
            releases = await _request_json(url, headers=headers)
        except Exception as exc:
            logger.warning("github_releases_failed", repo=repo, error=str(exc))
            continue
        if not isinstance(releases, list):
            continue
        for release in releases:
            findings.append(
                ResearchFinding(
                    source="github",
                    domain=domain,
                    title=f"{repo} {release.get('tag_name', '')}",
                    url=str(release.get("html_url", "")),
                    summary=str(release.get("name") or release.get("body") or "")[:600],
                    published_at=_to_datetime(release.get("published_at")),
                    metadata={"repo": repo, "kind": "release", "stars": 0},
                )
            )
    return findings


async def hackernews_search(
    query: str,
    max_results: int = 10,
    *,
    domain: str,
) -> list[ResearchFinding]:
    """Search Hacker News using Algolia API."""
    url = "https://hn.algolia.com/api/v1/search?" + urlencode(
        {
            "query": query,
            "tags": "story",
            "hitsPerPage": max(1, min(max_results, 20)),
        }
    )
    payload = await _request_json(url)
    findings: list[ResearchFinding] = []
    for hit in payload.get("hits", []):
        url_value = str(hit.get("url") or "")
        if not url_value:
            continue
        findings.append(
            ResearchFinding(
                source="hn",
                domain=domain,
                title=str(hit.get("title") or hit.get("story_title") or "")[:300],
                url=url_value,
                summary=str(hit.get("story_text") or hit.get("comment_text") or "")[:600],
                published_at=_to_datetime(hit.get("created_at")),
                metadata={"points": int(hit.get("points", 0))},
            )
        )
    return findings


async def reddit_search(
    query: str,
    subreddits: list[str],
    max_results: int = 10,
    *,
    domain: str,
) -> list[ResearchFinding]:
    """Search Reddit posts through public JSON endpoints."""
    headers = {"User-Agent": "Emergent-Agent/0.1"}
    findings: list[ResearchFinding] = []
    for subreddit in subreddits[:5]:
        url = f"https://www.reddit.com/r/{subreddit}/search.json?" + urlencode(
            {
                "q": query,
                "restrict_sr": 1,
                "sort": "relevance",
                "t": "week",
                "limit": max(1, min(max_results, 20)),
            }
        )
        try:
            payload = await _request_json(url, headers=headers)
        except Exception as exc:
            logger.warning("reddit_search_failed", subreddit=subreddit, error=str(exc))
            continue
        children = payload.get("data", {}).get("children", [])
        for child in children:
            data = child.get("data", {})
            permalink = str(data.get("permalink", ""))
            post_url = str(data.get("url", ""))
            resolved_url = post_url or (f"https://www.reddit.com{permalink}" if permalink else "")
            if not resolved_url:
                continue
            findings.append(
                ResearchFinding(
                    source="reddit",
                    domain=domain,
                    title=str(data.get("title", ""))[:300],
                    url=resolved_url,
                    summary=str(data.get("selftext", ""))[:600],
                    published_at=datetime.fromtimestamp(float(data.get("created_utc", 0)), tz=UTC),
                    metadata={"score": int(data.get("score", 0)), "subreddit": subreddit},
                )
            )
    return findings


async def rss_fetch(feed_urls: list[str], *, domain: str) -> list[ResearchFinding]:
    """Fetch and parse RSS/Atom feeds."""
    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser_missing", source="rss")
        return []

    findings: list[ResearchFinding] = []
    for feed_url in feed_urls:
        try:
            xml_payload = await _request_text(feed_url)
        except Exception as exc:
            logger.warning("rss_fetch_failed", feed_url=feed_url, error=str(exc))
            continue

        feed = feedparser.parse(xml_payload)
        for entry in feed.entries[:10]:
            findings.append(
                ResearchFinding(
                    source="rss",
                    domain=domain,
                    title=str(getattr(entry, "title", ""))[:300],
                    url=str(getattr(entry, "link", "")),
                    summary=str(getattr(entry, "summary", ""))[:600],
                    published_at=_to_datetime(getattr(entry, "published", None)),
                    metadata={"feed": feed_url},
                )
            )
    return findings


async def tavily_search(
    query: str,
    max_results: int = 10,
    topic: str = "general",
    time_range: str = "week",
    *,
    domain: str,
) -> list[ResearchFinding]:
    """Search the web through Tavily when API key is available."""
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        logger.warning("tavily_key_missing")
        return []

    try:
        from tavily import AsyncTavilyClient
    except ImportError:
        logger.warning("tavily_sdk_missing")
        return []

    client = AsyncTavilyClient(api_key=api_key)
    payload = await client.search(
        query=query,
        topic=topic,
        search_depth="basic",
        max_results=max(1, min(max_results, 20)),
        time_range=time_range,
    )
    findings: list[ResearchFinding] = []
    for item in payload.get("results", []):
        findings.append(
            ResearchFinding(
                source="tavily",
                domain=domain,
                title=str(item.get("title", ""))[:300],
                url=str(item.get("url", "")),
                summary=str(item.get("content", ""))[:600],
                published_at=None,
                metadata={"score": float(item.get("score", 0.0))},
            )
        )
    return findings


async def _render_research_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No se encontraron findings."
    lines = ["Research findings:"]
    for idx, row in enumerate(rows, start=1):
        lines.extend(
            [
                f"[{idx}] {row.get('title', '')}",
                (
                    f"score={float(row.get('relevance_score', 0.0)):.2f} "
                    f"source={row.get('source', '')}"
                ),
                str(row.get("url", "")),
                "",
            ]
        )
    return "\n".join(lines)


def make_research_search_handler(
    store: MemoryStore, retriever: SemanticRetriever
) -> Callable[..., Any]:
    async def research_search(tool_input: dict[str, Any]) -> str:
        query = str(tool_input.get("query", "")).strip()
        domain = str(tool_input.get("domain", "")).strip()
        limit = max(1, min(int(tool_input.get("limit", 10)), 20))
        if len(query) < 3:
            return "Error: query must be at least 3 characters"

        rows = await store.search_research(query=query, limit=limit, domain=domain or None)
        if rows:
            return await _render_research_rows(rows)

        semantic = await retriever.search_research(query=query, n_results=min(limit, 5))
        if not semantic:
            return "No se encontraron findings para esa busqueda."
        return await _render_research_rows(semantic)

    return research_search


def make_research_run_handler(worker: Any) -> Callable[..., Any]:
    async def research_run(tool_input: dict[str, Any]) -> str:
        topic = str(tool_input.get("topic", "")).strip()
        max_results = max(1, min(int(tool_input.get("max_results", 5)), 10))
        if not topic:
            return "Error: topic is required"
        highlights, rest = await worker.run_adhoc(topic=topic, max_results=max_results)
        top = [
            {
                "title": scored.finding.title,
                "url": scored.finding.url,
                "source": scored.finding.source,
                "relevance_score": scored.score,
            }
            for scored in highlights[:3]
        ]
        rows = top + [
            {
                "title": scored.finding.title,
                "url": scored.finding.url,
                "source": scored.finding.source,
                "relevance_score": scored.score,
            }
            for scored in rest[:3]
        ]
        return await _render_research_rows(rows)

    return research_run


RESEARCH_SEARCH_DEFINITION: ToolDefinitionDict = {
    "name": "research_search",
    "description": (
        "Search through stored research findings. Searches by keyword in titles and "
        "summaries, optionally filtered by domain. Returns scored results."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "domain": {
                "type": "string",
                "description": "Optional domain filter",
                "enum": [
                    "agent_architecture",
                    "rag_memory",
                    "python_ecosystem",
                    "security_safety",
                    "devops_infra",
                    "emergent_deps",
                ],
            },
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
}

RESEARCH_RUN_DEFINITION: ToolDefinitionDict = {
    "name": "research_run",
    "description": (
        "Run an ad-hoc research investigation on a specific topic. Searches across "
        "configured sources and stores resulting findings."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Topic to research",
                "maxLength": 200,
            },
            "max_results": {"type": "integer", "default": 5, "maximum": 10},
        },
        "required": ["topic"],
    },
}
