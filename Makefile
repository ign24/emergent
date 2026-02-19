.PHONY: run test test-e2e test-security lint typecheck dashboard triage clean install

run:
	uv run python -m emergent

install:
	uv sync

test:
	uv run pytest tests/ -k "not e2e" -v

test-e2e:
	uv run pytest tests/test_e2e/ -m "e2e and not expensive" -v

test-security:
	uv run pytest -m security -v

test-all:
	uv run pytest tests/ -v

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

format:
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

typecheck:
	uv run mypy src/

dashboard:
	uv run python -c "from emergent.observability.metrics import print_dashboard; import asyncio; asyncio.run(print_dashboard())"

triage:
	uv run python -c "from emergent.observability.metrics import print_triage; import asyncio; asyncio.run(print_triage())"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .mypy_cache .ruff_cache
