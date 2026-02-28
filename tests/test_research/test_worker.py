from __future__ import annotations

from datetime import UTC, datetime

from emergent.config import AgentConfig, EmergentSettings, ResearchConfig, TelegramConfig
from emergent.research.types import ResearchFinding
from emergent.research.worker import ResearchWorker


async def test_worker_run_persists_findings_and_writes_report(
    tmp_db, tmp_retriever, tmp_path
) -> None:
    settings = EmergentSettings(
        anthropic_api_key="",
        telegram=TelegramConfig(bot_token="", allowed_user_ids=[]),
        agent=AgentConfig(data_dir=str(tmp_path)),
        research=ResearchConfig(
            enabled=True,
            schedule="30 9 * * *",
            max_findings_per_domain=5,
            highlight_threshold=0.5,
            max_highlights=3,
            web_search_provider="tavily",
        ),
    )
    worker = ResearchWorker(store=tmp_db, retriever=tmp_retriever, settings=settings)

    async def _fake_fetch(max_results: int) -> list[ResearchFinding]:
        _ = max_results
        return [
            ResearchFinding(
                source="github",
                domain="emergent_deps",
                title="chroma release",
                url="https://example.com/chroma-release",
                summary="release with async improvements",
                published_at=datetime.now(UTC),
                metadata={"stars": 500},
            )
        ]

    worker._fetch_default_domains = _fake_fetch  # type: ignore[method-assign]

    await worker.run()

    rows = await tmp_db.search_research("chroma", limit=10)
    assert len(rows) == 1
    reports = list((tmp_path / "research").glob("*.md"))
    assert reports
