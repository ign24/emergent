"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from emergent.memory.retriever import SemanticRetriever
from emergent.memory.store import MemoryStore


@pytest.fixture
def tmp_db(tmp_path: Path) -> MemoryStore:
    """In-memory-style SQLite store using a temp file."""
    db_path = tmp_path / "test.db"
    store = MemoryStore(db_path)
    return store


@pytest.fixture
def tmp_retriever(tmp_path: Path) -> SemanticRetriever:
    """Retriever using temp chroma dir."""
    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    return SemanticRetriever(chroma_dir)
