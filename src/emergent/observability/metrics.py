"""Dashboard CLI — aggregate metrics queries over SQLite traces."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _get_conn(data_dir: str = "./data") -> sqlite3.Connection:
    db_path = Path(data_dir) / "emergent.db"
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _pct_bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


async def print_dashboard(data_dir: str = "./data") -> None:
    """Print observability dashboard to stdout."""
    try:
        conn = _get_conn(data_dir)
    except FileNotFoundError as e:
        print(f"[dashboard] {e}\nStart the agent first to generate data.")
        return

    print("\n" + "=" * 60)
    print("  EMERGENT — OBSERVABILITY DASHBOARD")
    print("=" * 60)

    # --- Request Volume ---
    print("\n📊 REQUEST VOLUME")
    for label, interval in [("24h", "-1 day"), ("7d", "-7 days"), ("30d", "-30 days")]:
        row = conn.execute(
            f"SELECT COUNT(*) as cnt, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as ok "
            f"FROM traces WHERE timestamp >= datetime('now', '{interval}')"
        ).fetchone()
        total = row["cnt"] or 0
        ok = row["ok"] or 0
        rate = (ok / total * 100) if total > 0 else 0
        status = "✅" if rate >= 90 else "⚠️" if rate >= 75 else "🚨"
        print(f"  {label:4s}: {total:4d} requests | {rate:5.1f}% success {status}")

    # --- Latency ---
    print("\n⚡ LATENCY (last 24h)")
    rows = conn.execute(
        "SELECT total_duration_ms FROM traces "
        "WHERE timestamp >= datetime('now', '-1 day') AND success=1 "
        "ORDER BY total_duration_ms"
    ).fetchall()
    if rows:
        durations = [r["total_duration_ms"] for r in rows if r["total_duration_ms"]]
        durations.sort()
        n = len(durations)
        p50 = durations[n // 2] if n > 0 else 0
        p95 = durations[int(n * 0.95)] if n > 0 else 0
        p50_s = p50 / 1000 if p50 else 0
        p95_s = p95 / 1000 if p95 else 0
        p50_status = "✅" if p50_s < 8 else "⚠️" if p50_s < 12 else "🚨"
        p95_status = "✅" if p95_s < 30 else "⚠️" if p95_s < 45 else "🚨"
        print(f"  p50: {p50_s:5.1f}s {p50_status}   p95: {p95_s:5.1f}s {p95_status}")
    else:
        print("  No data for last 24h")

    # --- Cost ---
    print("\n💰 COST")
    for label, interval in [("24h", "-1 day"), ("7d", "-7 days"), ("30d", "-30 days")]:
        row = conn.execute(
            f"SELECT SUM(total_cost_usd) as total, AVG(total_cost_usd) as avg "
            f"FROM traces WHERE timestamp >= datetime('now', '{interval}')"
        ).fetchone()
        total = row["total"] or 0
        avg = row["avg"] or 0
        avg_status = "✅" if avg < 0.05 else "⚠️" if avg < 0.08 else "💸"
        print(f"  {label:4s}: ${total:.4f} total | ${avg:.4f} avg/req {avg_status}")

    # --- Tool Usage ---
    print("\n🔧 TOOL USAGE (last 7d)")
    rows = conn.execute(
        "SELECT tools_called_json FROM traces "
        "WHERE timestamp >= datetime('now', '-7 days') AND tools_called_json IS NOT NULL"
    ).fetchall()
    tool_counts: dict[str, int] = {}
    for row in rows:
        try:
            tools = json.loads(row["tools_called_json"]) or []
            for t in tools:
                tool_counts[t] = tool_counts.get(t, 0) + 1
        except Exception:
            pass

    if tool_counts:
        total_calls = sum(tool_counts.values())
        for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1])[:8]:
            pct = count / total_calls * 100
            bar = _pct_bar(pct, 15)
            print(f"  {tool:<20} {bar} {pct:5.1f}% ({count})")

    # --- Errors ---
    print("\n❌ TOP ERRORS (last 7d)")
    rows = conn.execute(
        "SELECT error_message, COUNT(*) as cnt "
        "FROM traces WHERE error_message IS NOT NULL "
        "AND timestamp >= datetime('now', '-7 days') "
        "GROUP BY error_message ORDER BY cnt DESC LIMIT 5"
    ).fetchall()
    if rows:
        for row in rows:
            print(f"  [{row['cnt']:3d}x] {row['error_message'][:60]}")
    else:
        print("  No errors in last 7d ✅")

    # --- Expensive traces ---
    print("\n💸 MOST EXPENSIVE TRACES (last 7d)")
    rows = conn.execute(
        "SELECT id, total_cost_usd, total_duration_ms, tools_called_json "
        "FROM traces WHERE timestamp >= datetime('now', '-7 days') "
        "ORDER BY total_cost_usd DESC LIMIT 5"
    ).fetchall()
    if rows:
        for row in rows:
            tools = json.loads(row["tools_called_json"] or "[]")
            dur_s = (row["total_duration_ms"] or 0) / 1000
            print(f"  ${row['total_cost_usd']:.4f} | {dur_s:.1f}s | {', '.join(tools[:3])}")
    else:
        print("  No data")

    # --- Memory stats ---
    print("\n🧠 MEMORY SYSTEM")
    try:
        profile_count = conn.execute("SELECT COUNT(*) as cnt FROM user_profile").fetchone()["cnt"]
        summary_count = conn.execute("SELECT COUNT(*) as cnt FROM session_summaries").fetchone()[
            "cnt"
        ]
        conv_count = conn.execute("SELECT COUNT(*) as cnt FROM conversations").fetchone()["cnt"]
        print(f"  Profile entries:   {profile_count}")
        print(f"  Session summaries: {summary_count}")
        print(f"  Conversation turns: {conv_count}")
    except Exception:
        print("  Unable to read memory stats")

    print("\n" + "=" * 60 + "\n")
    conn.close()


async def print_triage(data_dir: str = "./data") -> None:
    """Print weekly failure triage report."""
    try:
        conn = _get_conn(data_dir)
    except FileNotFoundError as e:
        print(f"[triage] {e}")
        return

    print("\n" + "=" * 60)
    print("  EMERGENT — WEEKLY TRIAGE REPORT")
    print("=" * 60)

    print("\n🔍 FAILURE PATTERNS (last 7d)")
    rows = conn.execute(
        "SELECT error_message, COUNT(*) as cnt, MIN(id) as example_trace_id "
        "FROM traces WHERE error_message IS NOT NULL "
        "AND timestamp >= datetime('now', '-7 days') "
        "GROUP BY error_message ORDER BY cnt DESC"
    ).fetchall()
    if rows:
        for row in rows:
            print(f"  [{row['cnt']:3d}x] {row['error_message'][:60]}")
            print(f"         Example trace: {row['example_trace_id']}")
    else:
        print("  No failures this week ✅")

    print("\n📈 METRIC TRENDS (7d vs previous 7d)")
    for label, col, fmt in [
        ("Success rate", "AVG(CASE WHEN success=1 THEN 1.0 ELSE 0 END)", ".1%"),
        ("Avg cost/req", "AVG(total_cost_usd)", "$.4f"),
    ]:
        cur = (
            conn.execute(
                f"SELECT {col} as val FROM traces WHERE timestamp >= datetime('now', '-7 days')"
            ).fetchone()["val"]
            or 0
        )
        prev = (
            conn.execute(
                f"SELECT {col} as val FROM traces "
                "WHERE timestamp BETWEEN datetime('now', '-14 days') AND datetime('now', '-7 days')"
            ).fetchone()["val"]
            or 0
        )
        delta = cur - prev
        arrow = "↑" if delta > 0 else "↓" if delta < 0 else "→"
        print(f"  {label:<20} {cur:{fmt}} {arrow} (prev: {prev:{fmt}})")

    print("\n💰 COST PROJECTION")
    row = conn.execute(
        "SELECT AVG(total_cost_usd) as avg_cost, COUNT(*) as cnt "
        "FROM traces WHERE timestamp >= datetime('now', '-7 days')"
    ).fetchone()
    avg_cost = row["avg_cost"] or 0
    weekly_cnt = row["cnt"] or 0
    monthly_projection = avg_cost * weekly_cnt * 4
    print(f"  Last 7d: {weekly_cnt} requests @ ${avg_cost:.4f} avg")
    print(f"  Monthly projection: ${monthly_projection:.2f}")

    print("\n" + "=" * 60 + "\n")
    conn.close()
