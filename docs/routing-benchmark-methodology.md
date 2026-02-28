# Routing Benchmark Methodology (Bias-Resistant)

This document defines how to evaluate routing policy changes without tuning the benchmark to make one strategy win.

## Goals

1. Keep Haiku-like cost profile for common/simple tasks.
2. Keep Sonnet-like quality profile for complex tasks.
3. Avoid manual prompt cherry-picking.

## Benchmark Design

- Use representative cases in `docs/ab-cases-representative.jsonl`.
- Preserve category distribution aligned with observed usage:
  - `simple` and `file_exploration`: frequent and cost-sensitive
  - `analysis`, `web_research`, `file_ops`: quality-sensitive
  - `safety`: refusal and boundary checks
- Compare 3 arms in every run:
  - `HAIKU_FIXED`
  - `SONNET_FIXED`
  - `AUTO`

## Run Command

```bash
uv run python scripts/run_routing_three_arm.py \
  --cases-file docs/ab-cases-representative.jsonl \
  --session-haiku eval_haiku_rep \
  --session-sonnet eval_sonnet_rep \
  --session-auto eval_auto_rep \
  --reset-sessions \
  --gate
```

## Three-Arm Gate

AUTO passes only if all checks pass:

- overall success >= HAIKU_FIXED - 1%
- overall cost/success <= HAIKU_FIXED + 20%
- simple-category p95 <= HAIKU_FIXED simple-category p95 + 10%
- complex-category quality >= SONNET_FIXED complex-category quality - 5%

## Why This Is Harder To Game

- AUTO is compared against both cheap and strong fixed baselines.
- Category-aware checks prevent a strategy from winning by only optimizing one task type.
- The same case file is reused across all arms and releases.
