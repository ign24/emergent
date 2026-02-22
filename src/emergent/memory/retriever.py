"""ChromaDB semantic retrieval (L1a)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

MIN_CHUNK_CHARS = 50
CHUNK_SIZE = 300  # tokens approx ~1200 chars
CHUNK_OVERLAP = 50


def _chunk_text(text: str, chunk_size: int = 1200, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap
    return chunks


class SemanticRetriever:
    """ChromaDB-backed semantic memory retrieval."""

    def __init__(self, chroma_dir: str | Path) -> None:
        self._chroma_dir = str(chroma_dir)
        self._client: Any = None
        self._collection: Any = None
        self._research_collection: Any = None

    def _ensure_initialized(self) -> bool:
        """Lazy initialization of ChromaDB. Returns False if unavailable."""
        if self._collection is not None and self._research_collection is not None:
            return True
        try:
            import chromadb

            self._client = chromadb.PersistentClient(path=self._chroma_dir)
            self._collection = self._client.get_or_create_collection(
                name="conversations",
                metadata={"hnsw:space": "cosine"},
            )
            self._research_collection = self._client.get_or_create_collection(
                name="research",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("chromadb_initialized", path=self._chroma_dir)
            return True
        except Exception as e:
            logger.warning("chromadb_init_failed", error=str(e))
            return False

    async def upsert_session(
        self,
        session_id: str,
        turns: list[dict[str, str]],
        timestamp: float | None = None,
    ) -> int:
        """Index a session's conversation turns into ChromaDB."""
        if not self._ensure_initialized():
            return 0

        ts = timestamp or time.time()
        docs_added = 0

        for i, turn in enumerate(turns):
            content = turn.get("content", "")
            if len(content) < MIN_CHUNK_CHARS:
                continue

            chunks = _chunk_text(content)
            for j, chunk in enumerate(chunks):
                doc_id = f"{session_id}_{i}_{j}"
                try:
                    self._collection.upsert(
                        ids=[doc_id],
                        documents=[chunk],
                        metadatas=[
                            {
                                "session_id": session_id,
                                "role": turn.get("role", "unknown"),
                                "turn_index": i,
                                "timestamp": ts,
                            }
                        ],
                    )
                    docs_added += 1
                except Exception as e:
                    logger.warning("chromadb_upsert_error", error=str(e), doc_id=doc_id)

        logger.info("chromadb_session_indexed", session_id=session_id, docs=docs_added)
        return docs_added

    async def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Search for semantically similar memories."""
        if not self._ensure_initialized():
            logger.warning("chromadb_unavailable_returning_empty")
            return []

        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k, 5),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            logger.warning("chromadb_search_failed", error=str(e))
            return []

        if not results["documents"] or not results["documents"][0]:
            return []

        output: list[dict[str, Any]] = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
            strict=False,
        ):
            # cosine distance → similarity score (1 = identical, 0 = unrelated)
            score = 1.0 - dist
            output.append(
                {
                    "content": doc,
                    "relevance_score": round(score, 3),
                    "session_id_source": meta.get("session_id", ""),
                    "timestamp": meta.get("timestamp", 0),
                }
            )

        logger.info("chromadb_search_done", query_len=len(query), results=len(output))
        return output

    async def get_relevant_memories_as_text(
        self, query: str, top_k: int = 3, min_score: float = 0.3
    ) -> list[str]:
        """Return memory snippets as plain text for context injection."""
        results = await self.search(query, top_k=top_k)
        return [r["content"] for r in results if r["relevance_score"] >= min_score]

    async def upsert_research_findings(self, findings: list[Any]) -> int:
        """Index research findings in dedicated research collection."""
        if not self._ensure_initialized():
            return 0

        docs_added = 0
        now = time.time()
        for idx, item in enumerate(findings):
            finding = getattr(item, "finding", item)
            url = str(getattr(finding, "url", ""))
            title = str(getattr(finding, "title", ""))
            summary = str(getattr(finding, "summary", ""))
            source = str(getattr(finding, "source", "unknown"))
            domain = str(getattr(finding, "domain", "unknown"))
            score_raw = getattr(item, "score", 0.0)
            score = float(score_raw) if isinstance(score_raw, (float, int)) else 0.0
            if not url or not title:
                continue

            doc_id = f"research_{abs(hash(url))}_{idx}"
            content = f"{title}\n\n{summary}\n\nURL: {url}"
            try:
                self._research_collection.upsert(
                    ids=[doc_id],
                    documents=[content[:4000]],
                    metadatas=[
                        {
                            "url": url,
                            "title": title[:500],
                            "source": source,
                            "domain": domain,
                            "score": score,
                            "timestamp": now,
                        }
                    ],
                )
                docs_added += 1
            except Exception as e:
                logger.warning("chromadb_research_upsert_error", error=str(e), doc_id=doc_id)
        return docs_added

    async def search_research(self, query: str, n_results: int = 5) -> list[dict[str, Any]]:
        """Search semantic research collection."""
        if not self._ensure_initialized():
            return []
        try:
            results = self._research_collection.query(
                query_texts=[query],
                n_results=min(max(1, n_results), 10),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            logger.warning("chromadb_research_search_failed", error=str(e))
            return []

        docs = results.get("documents", [[]])
        if not docs or not docs[0]:
            return []

        output: list[dict[str, Any]] = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
            strict=False,
        ):
            score = round(1.0 - float(dist), 3)
            output.append(
                {
                    "title": meta.get("title", ""),
                    "url": meta.get("url", ""),
                    "source": meta.get("source", ""),
                    "domain": meta.get("domain", ""),
                    "summary": doc[:500],
                    "relevance_score": score,
                }
            )
        return output
