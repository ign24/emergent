"""Autonomous research worker orchestrator."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from emergent.config import EmergentSettings
from emergent.memory.retriever import SemanticRetriever
from emergent.memory.store import MemoryStore
from emergent.research.formatter import generate_markdown_report, generate_telegram_digest
from emergent.research.scoring import score_finding, separate_highlights
from emergent.research.sources import RESEARCH_DOMAINS, RSS_FEEDS
from emergent.research.types import ResearchFinding, ScoredFinding
from emergent.tools import research as research_tools

logger = structlog.get_logger(__name__)


@dataclass
class ResearchWorker:
    """Coordinates fetch, score, persistence, indexing, and notification."""

    store: MemoryStore
    retriever: SemanticRetriever
    settings: EmergentSettings
    telegram_notify: Any | None = None

    async def run(self) -> None:
        """Execute scheduled research run."""
        run_id = str(uuid.uuid4())[:8]
        log = logger.bind(run_id=run_id)
        log.info("research_run_start")

        findings = await self._fetch_default_domains(
            max_results=self.settings.research.max_findings_per_domain
        )
        if not findings:
            log.warning("research_no_findings")
            return

        await self._finalize_run(run_id=run_id, findings=findings)

    async def run_adhoc(
        self, topic: str, max_results: int = 5
    ) -> tuple[list[ScoredFinding], list[ScoredFinding]]:
        """Run ad-hoc investigation for a user-provided topic."""
        run_id = str(uuid.uuid4())[:8]
        log = logger.bind(run_id=run_id, topic=topic)
        log.info("research_run_adhoc_start")

        tasks = [
            research_tools.tavily_search(
                topic, max_results=max_results, topic="general", domain="adhoc"
            ),
            research_tools.github_search(topic, max_results=max_results, domain="adhoc"),
            research_tools.hackernews_search(topic, max_results=max_results, domain="adhoc"),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        findings = self._flatten_findings(results)
        if not findings:
            return [], []
        scored = self._score_all(findings)
        highlights, rest = separate_highlights(
            scored,
            threshold=self.settings.research.highlight_threshold,
            max_highlights=self.settings.research.max_highlights,
        )
        await self._persist_and_index(run_id=run_id, scored=scored, highlights=highlights)
        return highlights, rest

    async def _fetch_default_domains(self, max_results: int) -> list[ResearchFinding]:
        tasks: list[asyncio.Future[Any] | asyncio.Task[Any] | Any] = []

        rss_by_domain: dict[str, list[str]] = {}
        for item in RSS_FEEDS:
            rss_by_domain.setdefault(item["domain"], []).append(item["url"])

        for domain, cfg in RESEARCH_DOMAINS.items():
            for query in cfg.get("arxiv_queries", []):
                tasks.append(
                    research_tools.arxiv_search(
                        query,
                        max_results=max_results,
                        categories=cfg.get("arxiv_categories", []),
                        domain=domain,
                    )
                )
            for query in cfg.get("github_queries", []):
                tasks.append(
                    research_tools.github_search(query, max_results=max_results, domain=domain)
                )
            for query in cfg.get("hn_queries", []):
                tasks.append(
                    research_tools.hackernews_search(query, max_results=max_results, domain=domain)
                )
            for query in cfg.get("tavily_queries", []):
                tasks.append(
                    research_tools.tavily_search(query, max_results=max_results, domain=domain)
                )
            subreddits = cfg.get("subreddits", [])
            if subreddits:
                tasks.append(
                    research_tools.reddit_search(
                        query=domain.replace("_", " "),
                        subreddits=list(subreddits),
                        max_results=max_results,
                        domain=domain,
                    )
                )
            releases_repos = cfg.get("releases_repos", [])
            if releases_repos:
                tasks.append(research_tools.github_releases(list(releases_repos), domain=domain))
            if domain in rss_by_domain:
                tasks.append(research_tools.rss_fetch(rss_by_domain[domain], domain=domain))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        findings = self._flatten_findings(results)
        return self._deduplicate(findings)

    def _flatten_findings(self, results: list[Any]) -> list[ResearchFinding]:
        findings: list[ResearchFinding] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("research_source_failed", error=str(result))
                continue
            findings.extend(result)
        return findings

    def _deduplicate(self, findings: list[ResearchFinding]) -> list[ResearchFinding]:
        seen: set[str] = set()
        deduped: list[ResearchFinding] = []
        for finding in findings:
            url = finding.url.strip()
            if not url or url in seen:
                continue
            seen.add(url)
            deduped.append(finding)
        return deduped

    def _score_all(self, findings: list[ResearchFinding]) -> list[ScoredFinding]:
        scored: list[ScoredFinding] = []
        for finding in findings:
            keywords = RESEARCH_DOMAINS.get(finding.domain, {}).get("keywords", [])
            scored.append(
                ScoredFinding(finding=finding, score=score_finding(finding, list(keywords)))
            )
        return sorted(scored, key=lambda item: item.score, reverse=True)

    async def _persist_and_index(
        self,
        *,
        run_id: str,
        scored: list[ScoredFinding],
        highlights: list[ScoredFinding],
    ) -> int:
        rows = self._to_store_rows(run_id=run_id, scored=scored, highlights=highlights)
        inserted = await self.store.save_research_findings(rows)
        try:
            await self.retriever.upsert_research_findings(highlights)
        except Exception as exc:
            logger.warning("research_chroma_upsert_failed", error=str(exc))
        return inserted

    def _to_store_rows(
        self,
        *,
        run_id: str,
        scored: list[ScoredFinding],
        highlights: list[ScoredFinding],
    ) -> list[dict[str, Any]]:
        highlighted_urls = {item.finding.url for item in highlights}
        rows: list[dict[str, Any]] = []
        for item in scored:
            finding = item.finding
            rows.append(
                {
                    "id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "domain": finding.domain,
                    "source": finding.source,
                    "title": finding.title,
                    "url": finding.url,
                    "summary": finding.summary,
                    "relevance_score": item.score,
                    "is_highlight": finding.url in highlighted_urls,
                    "published_at": finding.published_at.isoformat()
                    if finding.published_at
                    else None,
                    "metadata": finding.metadata,
                }
            )
        return rows

    async def _finalize_run(self, *, run_id: str, findings: list[ResearchFinding]) -> None:
        scored = self._score_all(findings)
        highlights, rest = separate_highlights(
            scored,
            threshold=self.settings.research.highlight_threshold,
            max_highlights=self.settings.research.max_highlights,
        )

        inserted = await self._persist_and_index(
            run_id=run_id, scored=scored, highlights=highlights
        )
        report_path = await self._write_markdown_report(
            run_id=run_id, highlights=highlights, rest=rest
        )
        await self._send_telegram_digest(highlights=highlights, rest=rest, report_path=report_path)

        logger.info(
            "research_run_done",
            run_id=run_id,
            total_findings=len(scored),
            highlights=len(highlights),
            new_persisted=inserted,
        )

    async def _write_markdown_report(
        self,
        *,
        run_id: str,
        highlights: list[ScoredFinding],
        rest: list[ScoredFinding],
    ) -> Path:
        data_dir = Path(self.settings.agent.data_dir)
        reports_dir = data_dir / "research"
        report_content = generate_markdown_report(
            run_id=run_id,
            highlights=highlights,
            rest=rest,
            total_sources=6,
            report_style=self.settings.research.report_style,
        )
        await asyncio.to_thread(reports_dir.mkdir, parents=True, exist_ok=True)
        filename = f"{datetime.now(UTC):%Y-%m-%d}-{run_id}.md"
        path = reports_dir / filename
        await asyncio.to_thread(path.write_text, report_content, encoding="utf-8")
        return path

    async def _send_telegram_digest(
        self,
        *,
        highlights: list[ScoredFinding],
        rest: list[ScoredFinding],
        report_path: Path,
    ) -> None:
        if self.telegram_notify is None:
            return
        digest = generate_telegram_digest(
            highlights=highlights,
            rest_count=len(rest),
            report_relpath=str(report_path),
        )

        for chat_id in self.settings.telegram.allowed_user_ids:
            try:
                await self.telegram_notify._bot.send_message(chat_id=chat_id, text=digest)
            except Exception as exc:
                logger.warning("research_telegram_send_failed", chat_id=chat_id, error=str(exc))
