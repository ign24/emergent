"""Run an automated A/B prompt benchmark and store traces.

This script executes the same prompt list against two scenarios and then prints a
metric comparison using the traces table.

Examples:
    uv run python scripts/run_ab_prompts.py \
      --prompts-file docs/ab-prompts.example.txt \
      --session-a terminal_fixed \
      --session-b terminal_auto \
      --model-a claude-haiku-4-5-20251001 \
      --model-b claude-sonnet-4-20250514 \
      --reset-sessions

    uv run python scripts/run_ab_prompts.py \
      --prompts-file docs/ab-prompts.example.txt \
      --cases-file docs/ab-cases.example.jsonl \
      --session-a eval_fixed \
      --session-b eval_auto \
      --routing-a fixed \
      --routing-b auto
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from emergent.agent.context import ContextBuilder
from emergent.agent.runtime import AgentRuntime
from emergent.config import EmergentSettings, get_settings
from emergent.memory.retriever import SemanticRetriever
from emergent.memory.store import MemoryStore
from emergent.memory.summarizer import summarize_conversation


@dataclass(frozen=True)
class Scenario:
    label: str
    session_id: str
    provider: str
    model: str
    routing_mode: str


@dataclass(frozen=True)
class Metrics:
    requests: int
    success_requests: int
    success_rate: float
    avg_cost_per_success_usd: float
    avg_latency_ms: float
    p95_latency_ms: float
    avg_tokens: float
    avg_tool_calls: float
    quality_pass_rate: float | None


@dataclass(frozen=True)
class ReleaseGateResult:
    passed: bool
    checks: tuple[str, ...]


@dataclass(frozen=True)
class EvalCheck:
    kind: str
    value: str


@dataclass(frozen=True)
class PromptCase:
    prompt: str
    checks: tuple[EvalCheck, ...] = ()
    category: str = "general"


@dataclass(frozen=True)
class RunRecord:
    success: bool
    tokens: int
    latency_ms: float
    cost_usd: float
    tool_calls: int
    model_tier: str
    routing_reason: str
    quality_passed: bool | None
    category: str


def _load_prompts(file_path: Path) -> list[str]:
    if not file_path.exists():
        raise SystemExit(f"Prompts file not found: {file_path}")

    prompts: list[str] = []
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        prompts.append(line)

    if not prompts:
        raise SystemExit("Prompts file is empty after filtering comments/blanks")
    return prompts


def _load_cases(cases_file: Path) -> list[PromptCase]:
    if not cases_file.exists():
        raise SystemExit(f"Cases file not found: {cases_file}")

    cases: list[PromptCase] = []
    for line_no, raw in enumerate(cases_file.read_text(encoding="utf-8").splitlines(), start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            raise SystemExit(f"Invalid JSON in {cases_file}:{line_no}: {e}") from e

        prompt = str(payload.get("prompt", "")).strip()
        if not prompt:
            raise SystemExit(f"Missing prompt in {cases_file}:{line_no}")

        checks: list[EvalCheck] = []
        for key, kind in (
            ("expect_exact", "exact"),
            ("expect_contains", "contains"),
            ("expect_regex", "regex"),
            ("reject_contains", "not_contains"),
        ):
            for value in _collect_case_values(payload, key, cases_file, line_no):
                checks.append(EvalCheck(kind=kind, value=value))

        raw_category = str(payload.get("category", "general")).strip().lower()
        category = raw_category if raw_category else "general"

        cases.append(PromptCase(prompt=prompt, checks=tuple(checks), category=category))

    if not cases:
        raise SystemExit("Cases file is empty after filtering comments/blanks")
    return cases


def _evaluate_response(response_text: str, checks: tuple[EvalCheck, ...]) -> bool | None:
    if not checks:
        return None

    text = response_text.strip()
    text_lc = text.lower()
    for check in checks:
        value_lc = check.value.lower()
        if check.kind == "exact" and text != check.value:
            return False
        if check.kind == "contains" and value_lc not in text_lc:
            return False
        if check.kind == "not_contains" and value_lc in text_lc:
            return False
        if check.kind == "regex" and re.search(check.value, text, re.IGNORECASE) is None:
            return False
    return True


def _collect_case_values(
    payload: dict[str, object],
    key: str,
    cases_file: Path,
    line_no: int,
) -> list[str]:
    value = payload.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise SystemExit(
                    f"Invalid type for {key} in {cases_file}:{line_no}; expected string list"
                )
            text = item.strip()
            if text:
                values.append(text)
        return values
    raise SystemExit(
        f"Invalid type for {key} in {cases_file}:{line_no}; expected string or string list"
    )


def _clone_settings(
    base: EmergentSettings,
    provider: str,
    model: str,
    routing_mode: str,
) -> EmergentSettings:
    cloned = copy.deepcopy(base)
    cloned.agent.provider = provider
    cloned.agent.model = model
    cloned.agent.routing_enabled = routing_mode == "auto"
    return cloned


async def _run_scenario(
    *,
    base_settings: EmergentSettings,
    scenario: Scenario,
    cases: list[PromptCase],
    context_budget_tokens: int,
    summarize_at_pct: float,
) -> list[RunRecord]:
    settings = _clone_settings(
        base_settings,
        scenario.provider,
        scenario.model,
        scenario.routing_mode,
    )

    data_dir = Path(settings.agent.data_dir)
    db_path = data_dir / settings.memory.get("sqlite_db", "emergent.db")
    chroma_dir = data_dir / settings.memory.get("chroma_dir", "chroma")

    store = MemoryStore(db_path)
    retriever = SemanticRetriever(chroma_dir)
    context_builder = ContextBuilder(
        store=store,
        retriever=retriever,
        context_budget_tokens=context_budget_tokens,
        summarize_at_pct=summarize_at_pct,
        max_history_turns=int(settings.memory.get("max_history_turns", 12)),
        history_keep_after_summary=int(settings.memory.get("history_keep_after_summary", 4)),
    )
    runtime = AgentRuntime(settings=settings)

    print(
        f"\n[{scenario.label}] mode={scenario.routing_mode.upper()} "
        f"provider={scenario.provider} model={scenario.model}"
    )

    records: list[RunRecord] = []

    try:
        for i, case in enumerate(cases, start=1):
            prompt = case.prompt
            profile_text, memories, summary, history = await context_builder.build_context(
                session_id=scenario.session_id,
                current_query=prompt,
            )

            if context_builder.should_summarize(history):
                summary_client = runtime._client
                new_summary = await summarize_conversation(
                    summary_client,
                    history,
                    summary_model=settings.agent.haiku_model,
                )
                if new_summary:
                    await store.save_session_summary(scenario.session_id, new_summary)
                    summary = new_summary
                    history = history[-context_builder.history_keep_after_summary :]

            response_text, trace_data = await runtime.run(
                user_message=prompt,
                session_id=scenario.session_id,
                history=history,
                user_profile=profile_text,
                semantic_memories=memories,
                session_summary=summary,
            )

            await store.save_conversation_turn(scenario.session_id, "user", prompt)
            await store.save_conversation_turn(scenario.session_id, "assistant", response_text)
            await store.save_trace(trace_data)
            await retriever.upsert_session(
                session_id=scenario.session_id,
                turns=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": response_text},
                ],
            )

            success = "ok" if trace_data.get("success", False) else "error"
            tokens = int(trace_data.get("total_input_tokens", 0)) + int(
                trace_data.get("total_output_tokens", 0)
            )
            quality_passed = _evaluate_response(response_text, case.checks)
            quality_text = ""
            if quality_passed is True:
                quality_text = " · quality:pass"
            elif quality_passed is False:
                quality_text = " · quality:fail"

            records.append(
                RunRecord(
                    success=bool(trace_data.get("success", False)),
                    tokens=tokens,
                    latency_ms=float(trace_data.get("duration_ms", 0.0) or 0.0),
                    cost_usd=float(trace_data.get("total_cost_usd", 0.0) or 0.0),
                    tool_calls=len(trace_data.get("tools_called", [])),
                    model_tier=str(trace_data.get("model_tier", "unknown")),
                    routing_reason=str(trace_data.get("routing_reason", "unknown")),
                    quality_passed=quality_passed,
                    category=case.category,
                )
            )
            print(f"  {i:02d}/{len(cases)} {success} · {tokens} tok{quality_text}")
    finally:
        await runtime.close()

    return records


def _clear_session_data(db_path: Path, session_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM traces WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM tool_executions WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_summaries WHERE session_id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()


def _compute_metrics(records: list[RunRecord]) -> Metrics:
    if not records:
        return Metrics(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None)

    latencies = [r.latency_ms for r in records]
    latencies_sorted = sorted(latencies)
    p95_index = max(0, min(len(latencies_sorted) - 1, math.ceil(0.95 * len(latencies_sorted)) - 1))
    p95_latency = latencies_sorted[p95_index]

    success_rows = [r for r in records if r.success]
    success_count = len(success_rows)
    avg_cost_success = (
        sum(r.cost_usd for r in success_rows) / success_count if success_count else 0.0
    )
    avg_tokens = sum(r.tokens for r in records) / len(records)
    tool_calls_total = sum(r.tool_calls for r in records)

    quality_values = [r.quality_passed for r in records if r.quality_passed is not None]
    quality_pass_rate = None
    if quality_values:
        quality_pass_rate = sum(1 for q in quality_values if q) / len(quality_values)

    return Metrics(
        requests=len(records),
        success_requests=success_count,
        success_rate=success_count / len(records),
        avg_cost_per_success_usd=avg_cost_success,
        avg_latency_ms=sum(latencies) / len(records),
        p95_latency_ms=p95_latency,
        avg_tokens=avg_tokens,
        avg_tool_calls=tool_calls_total / len(records),
        quality_pass_rate=quality_pass_rate,
    )


def _distribution(records: list[RunRecord], attr: str) -> str:
    counts: dict[str, int] = {}
    for record in records:
        key = str(getattr(record, attr, "unknown") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return "-"
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{k}:{v}" for k, v in ordered)


def _compute_metrics_by_category(records: list[RunRecord]) -> dict[str, Metrics]:
    grouped: dict[str, list[RunRecord]] = {}
    for record in records:
        grouped.setdefault(record.category, []).append(record)
    return {category: _compute_metrics(rows) for category, rows in sorted(grouped.items())}


def _print_category_metrics(label: str, records: list[RunRecord]) -> None:
    metrics_by_category = _compute_metrics_by_category(records)
    if len(metrics_by_category) <= 1:
        return

    print(f"\nCategory metrics ({label})")
    print("| Category | Requests | Success | Cost/Success | p95 ms | Quality |")
    print("|---|---:|---:|---:|---:|---:|")
    for category, metrics in metrics_by_category.items():
        quality = "-"
        if metrics.quality_pass_rate is not None:
            quality = f"{metrics.quality_pass_rate:.2%}"
        print(
            "| "
            f"{category} | {metrics.requests} | {metrics.success_rate:.2%} | "
            f"${metrics.avg_cost_per_success_usd:.6f} | {metrics.p95_latency_ms:.0f} | {quality} |"
        )


def _print_comparison(label_a: str, a: Metrics, label_b: str, b: Metrics) -> None:
    print("\nRouting A/B Benchmark\n")
    print(f"| Metric | {label_a} | {label_b} | Delta (B-A) |")
    print("|---|---:|---:|---:|")

    def row(name: str, va: float, vb: float, *, pct: bool = False, money: bool = False) -> None:
        delta = vb - va
        if pct:
            print(f"| {name} | {va:.2%} | {vb:.2%} | {delta:+.2%} |")
        elif money:
            print(f"| {name} | ${va:.6f} | ${vb:.6f} | ${delta:+.6f} |")
        else:
            print(f"| {name} | {va:.2f} | {vb:.2f} | {delta:+.2f} |")

    row("Requests", float(a.requests), float(b.requests))
    row("Success Requests", float(a.success_requests), float(b.success_requests))
    row("Success Rate", a.success_rate, b.success_rate, pct=True)
    row(
        "Avg Cost / Success (USD)",
        a.avg_cost_per_success_usd,
        b.avg_cost_per_success_usd,
        money=True,
    )
    row("Avg Latency (ms)", a.avg_latency_ms, b.avg_latency_ms)
    row("p95 Latency (ms)", a.p95_latency_ms, b.p95_latency_ms)
    row("Avg Tokens", a.avg_tokens, b.avg_tokens)
    row("Avg Tool Calls", a.avg_tool_calls, b.avg_tool_calls)
    if a.quality_pass_rate is not None and b.quality_pass_rate is not None:
        row("Quality Pass Rate", a.quality_pass_rate, b.quality_pass_rate, pct=True)


def _evaluate_release_gate(
    *,
    baseline_label: str,
    baseline: Metrics,
    candidate_label: str,
    candidate: Metrics,
    max_success_drop_pct: float,
    min_cost_improvement_pct: float,
    max_p95_increase_pct: float,
) -> ReleaseGateResult:
    checks: list[str] = []
    passed = True

    success_threshold = baseline.success_rate - max_success_drop_pct
    success_ok = candidate.success_rate >= success_threshold
    checks.append(
        "success_rate "
        f"{candidate.success_rate:.2%} >= {success_threshold:.2%} "
        f"({candidate_label} vs {baseline_label})" + (" ✅" if success_ok else " ❌")
    )
    passed = passed and success_ok

    if baseline.quality_pass_rate is None or candidate.quality_pass_rate is None:
        quality_ok = False
        checks.append("quality_pass_rate unavailable (requires --cases-file with checks) ❌")
    else:
        quality_ok = candidate.quality_pass_rate >= baseline.quality_pass_rate
        checks.append(
            "quality_pass_rate "
            f"{candidate.quality_pass_rate:.2%} >= {baseline.quality_pass_rate:.2%} "
            f"({candidate_label} vs {baseline_label})" + (" ✅" if quality_ok else " ❌")
        )
    passed = passed and quality_ok

    cost_threshold = baseline.avg_cost_per_success_usd * (1.0 - min_cost_improvement_pct)
    cost_ok = candidate.avg_cost_per_success_usd <= cost_threshold
    checks.append(
        "avg_cost_per_success_usd "
        f"${candidate.avg_cost_per_success_usd:.6f} <= ${cost_threshold:.6f} "
        f"(target {min_cost_improvement_pct:.1%} better than {baseline_label})"
        + (" ✅" if cost_ok else " ❌")
    )
    passed = passed and cost_ok

    p95_threshold = baseline.p95_latency_ms * (1.0 + max_p95_increase_pct)
    p95_ok = candidate.p95_latency_ms <= p95_threshold
    checks.append(
        "p95_latency_ms "
        f"{candidate.p95_latency_ms:.0f} <= {p95_threshold:.0f} "
        f"(allowed +{max_p95_increase_pct:.1%} vs {baseline_label})" + (" ✅" if p95_ok else " ❌")
    )
    passed = passed and p95_ok

    return ReleaseGateResult(passed=passed, checks=tuple(checks))


async def _amain(args: argparse.Namespace) -> None:
    if args.cases_file:
        cases = _load_cases(Path(args.cases_file))
    else:
        cases = [PromptCase(prompt=p) for p in _load_prompts(Path(args.prompts_file))]

    get_settings.cache_clear()
    base_settings = get_settings()

    data_dir = Path(base_settings.agent.data_dir)
    db_path = data_dir / base_settings.memory.get("sqlite_db", "emergent.db")
    if args.reset_sessions:
        _clear_session_data(db_path, args.session_a)
        _clear_session_data(db_path, args.session_b)

    scenario_a = Scenario(
        label=args.label_a,
        session_id=args.session_a,
        provider=args.provider_a or base_settings.agent.provider,
        model=args.model_a or base_settings.agent.model,
        routing_mode=args.routing_a,
    )
    scenario_b = Scenario(
        label=args.label_b,
        session_id=args.session_b,
        provider=args.provider_b or base_settings.agent.provider,
        model=args.model_b or base_settings.agent.model,
        routing_mode=args.routing_b,
    )

    context_budget = int(base_settings.memory.get("context_budget_tokens", 20_000))
    summarize_pct = float(base_settings.memory.get("summarize_at_pct", 0.80))

    records_a = await _run_scenario(
        base_settings=base_settings,
        scenario=scenario_a,
        cases=cases,
        context_budget_tokens=context_budget,
        summarize_at_pct=summarize_pct,
    )
    records_b = await _run_scenario(
        base_settings=base_settings,
        scenario=scenario_b,
        cases=cases,
        context_budget_tokens=context_budget,
        summarize_at_pct=summarize_pct,
    )

    metrics_a = _compute_metrics(records_a)
    metrics_b = _compute_metrics(records_b)
    _print_comparison(args.label_a, metrics_a, args.label_b, metrics_b)
    print("\nRouting breakdown")
    print(f"- {args.label_a} tiers: {_distribution(records_a, 'model_tier')}")
    print(f"- {args.label_b} tiers: {_distribution(records_b, 'model_tier')}")
    print(f"- {args.label_a} reasons: {_distribution(records_a, 'routing_reason')}")
    print(f"- {args.label_b} reasons: {_distribution(records_b, 'routing_reason')}")
    _print_category_metrics(args.label_a, records_a)
    _print_category_metrics(args.label_b, records_b)

    if args.gate:
        gate = _evaluate_release_gate(
            baseline_label=args.label_a,
            baseline=metrics_a,
            candidate_label=args.label_b,
            candidate=metrics_b,
            max_success_drop_pct=args.gate_max_success_drop_pct / 100.0,
            min_cost_improvement_pct=args.gate_min_cost_improvement_pct / 100.0,
            max_p95_increase_pct=args.gate_max_p95_increase_pct / 100.0,
        )
        print("\nRelease Gate")
        for check in gate.checks:
            print(f"- {check}")
        print(f"- Result: {'PASS ✅' if gate.passed else 'FAIL ❌'}")
        if not gate.passed:
            raise SystemExit(2)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run A/B prompt benchmark end-to-end")
    parser.add_argument(
        "--prompts-file",
        default="",
        help="Text file with one prompt per line (required unless --cases-file provided)",
    )
    parser.add_argument(
        "--cases-file",
        default="",
        help="Optional JSONL with prompt + evaluation checks",
    )
    parser.add_argument("--session-a", required=True, help="Session id for baseline scenario")
    parser.add_argument("--session-b", required=True, help="Session id for experiment scenario")
    parser.add_argument("--label-a", default="A", help="Label for baseline scenario")
    parser.add_argument("--label-b", default="B", help="Label for experiment scenario")
    parser.add_argument("--provider-a", default="", help="Provider override for scenario A")
    parser.add_argument("--provider-b", default="", help="Provider override for scenario B")
    parser.add_argument("--model-a", default="", help="Model override for scenario A")
    parser.add_argument("--model-b", default="", help="Model override for scenario B")
    parser.add_argument(
        "--routing-a",
        choices=("fixed", "auto"),
        default="fixed",
        help="Routing mode for scenario A",
    )
    parser.add_argument(
        "--routing-b",
        choices=("fixed", "auto"),
        default="auto",
        help="Routing mode for scenario B",
    )
    parser.add_argument(
        "--reset-sessions",
        action="store_true",
        help="Delete existing traces/conversations for both sessions before running",
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Apply release gate checks and exit non-zero when failing",
    )
    parser.add_argument(
        "--gate-max-success-drop-pct",
        type=float,
        default=1.0,
        help="Maximum allowed success rate drop for candidate vs baseline",
    )
    parser.add_argument(
        "--gate-min-cost-improvement-pct",
        type=float,
        default=10.0,
        help="Minimum required cost-per-success improvement for candidate",
    )
    parser.add_argument(
        "--gate-max-p95-increase-pct",
        type=float,
        default=15.0,
        help="Maximum allowed p95 latency increase for candidate",
    )
    args = parser.parse_args()
    if not args.prompts_file and not args.cases_file:
        parser.error("Provide --prompts-file or --cases-file")
    return args


def main() -> None:
    args = _parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
