# Evaluation Harness

This document turns agent quality from "looks good" into measurable release criteria.

## Dimensions

- Goal fulfillment (35%): response satisfies the user request and required constraints.
- Logical consistency (20%): no contradictions between plan, tool execution, and final output.
- Execution efficiency (15%): unnecessary tool calls, retries, and token use are minimized.
- Plan quality and adherence (15%): reasonable plan exists and execution follows it.
- Safety and policy compliance (15%): no bypass of safety classifier, policy, or secret handling.

## Test Datasets

- `docs/ab-prompts.example.txt`: baseline prompt list for repeatable A/B runs.
- `docs/ab-cases.example.jsonl`: prompt cases with objective checks (`exact`, `contains`, `regex`, `not_contains`).
- `docs/ab-cases-representative.jsonl`: representative quality set.
- `tests/test_regression/`: historical incident reproductions and guards.

## Testing Pyramid

### Unit

- Scope: tool contracts, parser behavior, deterministic classification.
- Current examples:
  - `tests/test_tools/test_registry.py`
  - `tests/test_tools/test_shell.py`
  - `tests/test_memory/test_store.py`

### Integration

- Scope: runtime + tool registry + memory behavior in-process with mocked transport.
- Current examples:
  - `tests/test_integration/test_agent_loop.py`
  - `tests/test_channels/test_terminal.py`

### E2E

- Scope: live provider path with real runtime loop and latency envelope.
- Current examples:
  - `tests/test_e2e/test_agent_loop.py`

## Production KPIs

- Success rate
- p95 latency
- Average cost per successful request
- Quality pass rate (when JSONL cases include checks)
- Adoption/satisfaction (external product telemetry)

Primary benchmark tooling:

- `scripts/run_ab_prompts.py`
- `scripts/routing_benchmark.py`
- Optional three-arm comparison: `scripts/run_routing_three_arm.py`

## Release Thresholds

Default gate for A/B candidate release:

- Success rate: candidate must not drop more than 1.0 percentage point.
- Quality pass rate: candidate must be greater than or equal to baseline (requires `--cases-file`).
- Avg cost per success: candidate must improve by at least 10%.
- p95 latency: candidate must not increase more than 15%.

Reference command:

```bash
uv run python scripts/run_ab_prompts.py \
  --cases-file docs/ab-cases-representative.jsonl \
  --session-a eval_fixed \
  --session-b eval_auto \
  --label-a FIXED \
  --label-b AUTO \
  --routing-a fixed \
  --routing-b auto \
  --gate
```

## Historical Regression Suite

Purpose: encode previously observed failures so they cannot silently return.

- Location: `tests/test_regression/`
- Naming: `test_inc_<yyyy_mm_dd>_<short_slug>()`
- Each new incident should add one deterministic reproduction test.
- Releases are blocked on any regression failure.

### Minimum Necessary Profile (Lean)

Keep the regression suite small and operationally cheap.

- Exactly 6 critical cases at baseline:
  1. safety bypass via chain (`&& rm -rf`)
  2. safety bypass via inline execution (`python -c`)
  3. runtime runaway guard (`max_iterations`)
  4. message history order preservation
  5. secret storage block (`SECRETS_DETECTED`)
  6. SSRF localhost block (`SSRF_BLOCKED`)
- PR gate command: `uv run pytest -m regression -q`
- Target runtime for regression gate: under 3 minutes.
- Growth policy: add only for P0/P1 incidents.

Minimum metadata to include in each incident test docstring:

- Incident date
- User-visible symptom
- Root cause summary
- Expected invariant after fix

## Evaluation Spec Template

```md
## Evaluation Harness
- Dimensions:
- Test datasets:
- Unit coverage:
- Integration scenarios:
- E2E scenarios:
- Release thresholds:
```

## Verification Commands

```bash
uv run pytest -q
uv run pytest -m regression -q
```

## PR Policy

PRs must pass the fast CI gate before merge:

- Unit tests: `make test-unit`
- Integration tests: `make test-integration`
- Security tests: `make test-security`

Notes:

- `test-unit` currently runs `pytest -m "not integration and not e2e and not expensive"`, so
  `@pytest.mark.regression` tests are included in PR validation by default.
- Live E2E tests are intentionally excluded from PRs and run on non-PR events with secrets available.
