# Optimization Log

## Baseline Snapshot (2026-02-22)

- Total requests: 82
- Successful requests: 77
- Success rate: 93.9%
- Avg cost per successful request: $0.053472
- p95 latency: 68,459 ms
- Avg tokens per request: 15,269.4
- Token distribution:
  - 0-1k: 14
  - 1k-5k: 10
  - 5k-20k: 43
  - 20k+: 15

## Optimization Change

- Baseline cost/latency:
- Change applied:
- New cost/latency:
- Quality impact:
- Decision: keep/revert

## Routing Validation Procedure

1. In terminal, set a baseline session id and run the same prompt list:

   - `/session terminal_fixed`

2. Set the experiment session id and repeat the exact same prompts/order:

   - `/session terminal_auto`

3. Keep prompt order identical to get a fair A/B comparison.

4. Compare both sessions with:
```bash
uv run python scripts/routing_benchmark.py \
  --session-a terminal_fixed \
  --session-b terminal_auto \
  --label-a FIXED \
  --label-b AUTO
```

5. Accept routing if:
   - success rate does not regress materially
   - avg cost/success decreases
   - p95 latency stays within agreed budget

## Fully Automated Run (No Manual Session Switching)

```bash
uv run python scripts/run_ab_prompts.py \
  --prompts-file docs/ab-prompts.example.txt \
  --session-a terminal_fixed \
  --session-b terminal_auto \
  --label-a FIXED \
  --label-b AUTO \
  --routing-a fixed \
  --routing-b auto \
  --model-a claude-haiku-4-5-20251001 \
  --model-b claude-sonnet-4-20250514 \
  --reset-sessions
```

### Optional: quality checks with JSONL cases

```bash
uv run python scripts/run_ab_prompts.py \
  --prompts-file docs/ab-prompts.example.txt \
  --cases-file docs/ab-cases.example.jsonl \
  --session-a eval_fixed \
  --session-b eval_auto \
  --label-a FIXED \
  --label-b AUTO \
  --routing-a fixed \
  --routing-b auto \
  --model-a claude-haiku-4-5-20251001 \
  --model-b claude-sonnet-4-20250514 \
  --reset-sessions
```

This prints `Quality Pass Rate` plus routing tier/reason distributions.
