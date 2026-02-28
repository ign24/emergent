"""Run an unbiased three-arm routing benchmark.

This script evaluates three scenarios over the same case set:
1) HAIKU_FIXED  (cheap fixed baseline)
2) SONNET_FIXED (quality fixed baseline)
3) AUTO_ROUTING (haiku default + strong escalation)

It is designed to avoid benchmark bias by comparing AUTO against both ends of the
cost-quality spectrum instead of a single baseline.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from run_ab_prompts import (
    Metrics,
    PromptCase,
    ReleaseGateResult,
    Scenario,
    _clear_session_data,
    _compute_metrics,
    _distribution,
    _load_cases,
    _load_prompts,
    _run_scenario,
)

from emergent.config import get_settings


def _parse_categories(raw: str) -> set[str]:
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _subset_by_categories(records: list, categories: set[str]) -> list:
    return [record for record in records if getattr(record, "category", "") in categories]


def _print_scenarios_table(
    haiku_label: str,
    haiku_metrics: Metrics,
    sonnet_label: str,
    sonnet_metrics: Metrics,
    auto_label: str,
    auto_metrics: Metrics,
) -> None:
    print("\nRouting 3-Arm Benchmark\n")
    print(
        f"| Metric | {haiku_label} | {sonnet_label} | {auto_label} | "
        f"Delta AUTO-{haiku_label} | Delta AUTO-{sonnet_label} |"
    )
    print("|---|---:|---:|---:|---:|---:|")

    def row(
        name: str, v1: float, v2: float, v3: float, *, pct: bool = False, money: bool = False
    ) -> None:
        d13 = v3 - v1
        d23 = v3 - v2
        if pct:
            print(f"| {name} | {v1:.2%} | {v2:.2%} | {v3:.2%} | {d13:+.2%} | {d23:+.2%} |")
        elif money:
            print(f"| {name} | ${v1:.6f} | ${v2:.6f} | ${v3:.6f} | ${d13:+.6f} | ${d23:+.6f} |")
        else:
            print(f"| {name} | {v1:.2f} | {v2:.2f} | {v3:.2f} | {d13:+.2f} | {d23:+.2f} |")

    row(
        "Requests",
        float(haiku_metrics.requests),
        float(sonnet_metrics.requests),
        float(auto_metrics.requests),
    )
    row(
        "Success Requests",
        float(haiku_metrics.success_requests),
        float(sonnet_metrics.success_requests),
        float(auto_metrics.success_requests),
    )
    row(
        "Success Rate",
        haiku_metrics.success_rate,
        sonnet_metrics.success_rate,
        auto_metrics.success_rate,
        pct=True,
    )
    row(
        "Avg Cost / Success (USD)",
        haiku_metrics.avg_cost_per_success_usd,
        sonnet_metrics.avg_cost_per_success_usd,
        auto_metrics.avg_cost_per_success_usd,
        money=True,
    )
    row(
        "Avg Latency (ms)",
        haiku_metrics.avg_latency_ms,
        sonnet_metrics.avg_latency_ms,
        auto_metrics.avg_latency_ms,
    )
    row(
        "p95 Latency (ms)",
        haiku_metrics.p95_latency_ms,
        sonnet_metrics.p95_latency_ms,
        auto_metrics.p95_latency_ms,
    )
    row("Avg Tokens", haiku_metrics.avg_tokens, sonnet_metrics.avg_tokens, auto_metrics.avg_tokens)
    if (
        haiku_metrics.quality_pass_rate is not None
        and sonnet_metrics.quality_pass_rate is not None
        and auto_metrics.quality_pass_rate is not None
    ):
        row(
            "Quality Pass Rate",
            haiku_metrics.quality_pass_rate,
            sonnet_metrics.quality_pass_rate,
            auto_metrics.quality_pass_rate,
            pct=True,
        )


def _evaluate_three_arm_gate(
    *,
    auto_label: str,
    auto_all: Metrics,
    auto_simple: Metrics | None,
    auto_complex: Metrics | None,
    haiku_label: str,
    haiku_all: Metrics,
    haiku_simple: Metrics | None,
    sonnet_label: str,
    sonnet_complex: Metrics | None,
    max_success_drop_pct: float,
    max_cost_increase_pct_vs_haiku: float,
    max_simple_p95_increase_pct_vs_haiku: float,
    max_complex_quality_drop_pct_vs_sonnet: float,
) -> ReleaseGateResult:
    checks: list[str] = []
    passed = True

    success_threshold = haiku_all.success_rate - max_success_drop_pct
    success_ok = auto_all.success_rate >= success_threshold
    checks.append(
        f"overall success {auto_all.success_rate:.2%} >= {success_threshold:.2%} "
        f"({auto_label} vs {haiku_label})" + (" ✅" if success_ok else " ❌")
    )
    passed = passed and success_ok

    cost_threshold = haiku_all.avg_cost_per_success_usd * (1.0 + max_cost_increase_pct_vs_haiku)
    cost_ok = auto_all.avg_cost_per_success_usd <= cost_threshold
    checks.append(
        f"overall cost/success ${auto_all.avg_cost_per_success_usd:.6f} <= ${cost_threshold:.6f} "
        f"(allowed +{max_cost_increase_pct_vs_haiku:.1%} vs {haiku_label})"
        + (" ✅" if cost_ok else " ❌")
    )
    passed = passed and cost_ok

    if auto_simple is None or haiku_simple is None:
        checks.append("simple-category p95 gate skipped (missing simple categories) ⚠️")
    else:
        simple_p95_threshold = haiku_simple.p95_latency_ms * (
            1.0 + max_simple_p95_increase_pct_vs_haiku
        )
        simple_p95_ok = auto_simple.p95_latency_ms <= simple_p95_threshold
        checks.append(
            f"simple p95 {auto_simple.p95_latency_ms:.0f} <= {simple_p95_threshold:.0f} "
            f"(allowed +{max_simple_p95_increase_pct_vs_haiku:.1%} vs {haiku_label})"
            + (" ✅" if simple_p95_ok else " ❌")
        )
        passed = passed and simple_p95_ok

    if auto_complex is None or sonnet_complex is None:
        checks.append("complex-category quality gate skipped (missing complex categories) ⚠️")
    elif auto_complex.quality_pass_rate is None or sonnet_complex.quality_pass_rate is None:
        checks.append("complex-category quality unavailable (requires --cases-file checks) ❌")
        passed = False
    else:
        quality_threshold = (
            sonnet_complex.quality_pass_rate - max_complex_quality_drop_pct_vs_sonnet
        )
        quality_ok = auto_complex.quality_pass_rate >= quality_threshold
        checks.append(
            f"complex quality {auto_complex.quality_pass_rate:.2%} >= {quality_threshold:.2%} "
            f"({auto_label} vs {sonnet_label})" + (" ✅" if quality_ok else " ❌")
        )
        passed = passed and quality_ok

    return ReleaseGateResult(passed=passed, checks=tuple(checks))


async def _amain(args: argparse.Namespace) -> None:
    if args.cases_file:
        cases = _load_cases(Path(args.cases_file))
    else:
        cases = [PromptCase(prompt=p) for p in _load_prompts(Path(args.prompts_file))]

    get_settings.cache_clear()
    base_settings = get_settings()

    base_settings.agent.fast_provider = args.auto_fast_provider or base_settings.agent.fast_provider
    base_settings.agent.fast_model = args.auto_fast_model or base_settings.agent.fast_model
    base_settings.agent.strong_provider = (
        args.auto_strong_provider or base_settings.agent.strong_provider
    )
    base_settings.agent.strong_model = args.auto_strong_model or base_settings.agent.strong_model

    data_dir = Path(base_settings.agent.data_dir)
    db_path = data_dir / base_settings.memory.get("sqlite_db", "emergent.db")
    if args.reset_sessions:
        _clear_session_data(db_path, args.session_haiku)
        _clear_session_data(db_path, args.session_sonnet)
        _clear_session_data(db_path, args.session_auto)

    haiku = Scenario(
        label=args.label_haiku,
        session_id=args.session_haiku,
        provider=args.provider_haiku or base_settings.agent.provider,
        model=args.model_haiku,
        routing_mode="fixed",
    )
    sonnet = Scenario(
        label=args.label_sonnet,
        session_id=args.session_sonnet,
        provider=args.provider_sonnet or base_settings.agent.provider,
        model=args.model_sonnet,
        routing_mode="fixed",
    )
    auto = Scenario(
        label=args.label_auto,
        session_id=args.session_auto,
        provider=args.provider_auto or base_settings.agent.provider,
        model=args.model_auto,
        routing_mode="auto",
    )

    context_budget = int(base_settings.memory.get("context_budget_tokens", 20_000))
    summarize_pct = float(base_settings.memory.get("summarize_at_pct", 0.80))

    records_haiku = await _run_scenario(
        base_settings=base_settings,
        scenario=haiku,
        cases=cases,
        context_budget_tokens=context_budget,
        summarize_at_pct=summarize_pct,
    )
    records_sonnet = await _run_scenario(
        base_settings=base_settings,
        scenario=sonnet,
        cases=cases,
        context_budget_tokens=context_budget,
        summarize_at_pct=summarize_pct,
    )
    records_auto = await _run_scenario(
        base_settings=base_settings,
        scenario=auto,
        cases=cases,
        context_budget_tokens=context_budget,
        summarize_at_pct=summarize_pct,
    )

    metrics_haiku = _compute_metrics(records_haiku)
    metrics_sonnet = _compute_metrics(records_sonnet)
    metrics_auto = _compute_metrics(records_auto)

    _print_scenarios_table(
        args.label_haiku,
        metrics_haiku,
        args.label_sonnet,
        metrics_sonnet,
        args.label_auto,
        metrics_auto,
    )

    print("\nRouting breakdown")
    print(f"- {args.label_haiku} tiers: {_distribution(records_haiku, 'model_tier')}")
    print(f"- {args.label_sonnet} tiers: {_distribution(records_sonnet, 'model_tier')}")
    print(f"- {args.label_auto} tiers: {_distribution(records_auto, 'model_tier')}")
    print(f"- {args.label_haiku} reasons: {_distribution(records_haiku, 'routing_reason')}")
    print(f"- {args.label_sonnet} reasons: {_distribution(records_sonnet, 'routing_reason')}")
    print(f"- {args.label_auto} reasons: {_distribution(records_auto, 'routing_reason')}")

    simple_categories = _parse_categories(args.simple_categories)
    complex_categories = _parse_categories(args.complex_categories)

    simple_haiku = _subset_by_categories(records_haiku, simple_categories)
    simple_auto = _subset_by_categories(records_auto, simple_categories)
    complex_sonnet = _subset_by_categories(records_sonnet, complex_categories)
    complex_auto = _subset_by_categories(records_auto, complex_categories)

    def maybe_metrics(rows: list) -> Metrics | None:
        return _compute_metrics(rows) if rows else None

    if args.gate:
        gate = _evaluate_three_arm_gate(
            auto_label=args.label_auto,
            auto_all=metrics_auto,
            auto_simple=maybe_metrics(simple_auto),
            auto_complex=maybe_metrics(complex_auto),
            haiku_label=args.label_haiku,
            haiku_all=metrics_haiku,
            haiku_simple=maybe_metrics(simple_haiku),
            sonnet_label=args.label_sonnet,
            sonnet_complex=maybe_metrics(complex_sonnet),
            max_success_drop_pct=args.gate_max_success_drop_pct / 100.0,
            max_cost_increase_pct_vs_haiku=args.gate_max_cost_increase_pct_vs_haiku / 100.0,
            max_simple_p95_increase_pct_vs_haiku=args.gate_max_simple_p95_increase_pct_vs_haiku
            / 100.0,
            max_complex_quality_drop_pct_vs_sonnet=args.gate_max_complex_quality_drop_pct_vs_sonnet
            / 100.0,
        )
        print("\nThree-Arm Release Gate")
        for check in gate.checks:
            print(f"- {check}")
        print(f"- Result: {'PASS ✅' if gate.passed else 'FAIL ❌'}")
        if not gate.passed:
            raise SystemExit(2)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run unbiased three-arm routing benchmark")
    parser.add_argument("--prompts-file", default="")
    parser.add_argument("--cases-file", default="")

    parser.add_argument("--session-haiku", required=True)
    parser.add_argument("--session-sonnet", required=True)
    parser.add_argument("--session-auto", required=True)

    parser.add_argument("--label-haiku", default="HAIKU_FIXED")
    parser.add_argument("--label-sonnet", default="SONNET_FIXED")
    parser.add_argument("--label-auto", default="AUTO")

    parser.add_argument("--provider-haiku", default="")
    parser.add_argument("--provider-sonnet", default="")
    parser.add_argument("--provider-auto", default="")

    parser.add_argument("--model-haiku", default="claude-haiku-4-5-20251001")
    parser.add_argument("--model-sonnet", default="claude-sonnet-4-20250514")
    parser.add_argument("--model-auto", default="claude-haiku-4-5-20251001")

    parser.add_argument("--auto-fast-provider", default="")
    parser.add_argument("--auto-fast-model", default="")
    parser.add_argument("--auto-strong-provider", default="")
    parser.add_argument("--auto-strong-model", default="")

    parser.add_argument("--simple-categories", default="simple,file_exploration")
    parser.add_argument("--complex-categories", default="analysis,web_research,file_ops")

    parser.add_argument("--reset-sessions", action="store_true")
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--gate-max-success-drop-pct", type=float, default=1.0)
    parser.add_argument("--gate-max-cost-increase-pct-vs-haiku", type=float, default=20.0)
    parser.add_argument("--gate-max-simple-p95-increase-pct-vs-haiku", type=float, default=10.0)
    parser.add_argument("--gate-max-complex-quality-drop-pct-vs-sonnet", type=float, default=5.0)

    args = parser.parse_args()
    if not args.prompts_file and not args.cases_file:
        parser.error("Provide --prompts-file or --cases-file")
    return args


def main() -> None:
    args = _parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
