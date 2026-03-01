"""Compare routing A/B runs from traces in SQLite.

Usage example:
    uv run python scripts/routing_benchmark.py \
      --session-a terminal_auto \
      --session-b terminal_fixed
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


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


def _load_metrics(db_path: Path, session_id: str) -> Metrics:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT total_duration_ms, total_tokens, total_cost_usd, success, tools_called_json "
            "FROM traces WHERE session_id = ? ORDER BY timestamp ASC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return Metrics(
            requests=0,
            success_requests=0,
            success_rate=0.0,
            avg_cost_per_success_usd=0.0,
            avg_latency_ms=0.0,
            p95_latency_ms=0.0,
            avg_tokens=0.0,
            avg_tool_calls=0.0,
        )

    latencies = [float(row["total_duration_ms"] or 0.0) for row in rows]
    latencies_sorted = sorted(latencies)
    p95_index = max(0, min(len(latencies_sorted) - 1, math.ceil(0.95 * len(latencies_sorted)) - 1))
    p95_latency = latencies_sorted[p95_index]

    success_rows = [row for row in rows if int(row["success"] or 0) == 1]
    success_count = len(success_rows)
    avg_cost_success = (
        sum(float(row["total_cost_usd"] or 0.0) for row in success_rows) / success_count
        if success_count
        else 0.0
    )

    avg_tokens = sum(int(row["total_tokens"] or 0) for row in rows) / len(rows)

    tool_calls_total = 0
    for row in rows:
        raw = row["tools_called_json"]
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            tool_calls_total += len(parsed)

    return Metrics(
        requests=len(rows),
        success_requests=success_count,
        success_rate=success_count / len(rows),
        avg_cost_per_success_usd=avg_cost_success,
        avg_latency_ms=sum(latencies) / len(latencies),
        p95_latency_ms=p95_latency,
        avg_tokens=avg_tokens,
        avg_tool_calls=tool_calls_total / len(rows),
    )


def _print_table(label_a: str, a: Metrics, label_b: str, b: Metrics) -> None:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare routing A/B metrics from traces table")
    parser.add_argument("--db", default="data/emergent.db", help="Path to SQLite database")
    parser.add_argument("--session-a", required=True, help="Session id for baseline run")
    parser.add_argument("--session-b", required=True, help="Session id for experiment run")
    parser.add_argument("--label-a", default="A", help="Label for baseline")
    parser.add_argument("--label-b", default="B", help="Label for experiment")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of table")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    metrics_a = _load_metrics(db_path, args.session_a)
    metrics_b = _load_metrics(db_path, args.session_b)

    if args.json:
        print(
            json.dumps(
                {
                    "label_a": args.label_a,
                    "label_b": args.label_b,
                    "session_a": args.session_a,
                    "session_b": args.session_b,
                    "metrics_a": asdict(metrics_a),
                    "metrics_b": asdict(metrics_b),
                },
                indent=2,
            )
        )
        return

    _print_table(args.label_a, metrics_a, args.label_b, metrics_b)


if __name__ == "__main__":
    main()
