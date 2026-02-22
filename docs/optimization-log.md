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

1. Run baseline session in FIXED mode (same prompts each run).
2. Run experiment session in AUTO mode (same prompts, same order).
3. Compare both sessions with:

```bash
uv run python scripts/routing_benchmark.py \
  --session-a terminal_fixed \
  --session-b terminal_auto \
  --label-a FIXED \
  --label-b AUTO
```

4. Accept routing if:
   - success rate does not regress materially
   - avg cost/success decreases
   - p95 latency stays within agreed budget
