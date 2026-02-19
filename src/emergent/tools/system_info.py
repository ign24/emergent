"""System information tool — CPU, RAM, disk, processes."""

from __future__ import annotations

import time
from typing import Any

import psutil
import structlog

logger = structlog.get_logger(__name__)

_CACHE: dict[str, Any] = {}
_CACHE_TTL = 30  # seconds


async def system_info(tool_input: dict[str, Any]) -> str:
    """Return system metrics snapshot."""
    now = time.monotonic()

    # Cache with 30s TTL
    if "data" in _CACHE and now - _CACHE.get("ts", 0) < _CACHE_TTL:
        logger.debug("system_info_cache_hit")
        return _CACHE["data"]

    cpu_percent = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    uptime_seconds = time.time() - psutil.boot_time()

    # Top 5 processes by CPU
    processes: list[dict[str, Any]] = []
    for proc in sorted(
        psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
        key=lambda p: p.info.get("cpu_percent") or 0,
        reverse=True,
    )[:5]:
        try:
            processes.append(
                {
                    "pid": proc.info["pid"],
                    "name": proc.info["name"],
                    "cpu_pct": round(proc.info.get("cpu_percent") or 0, 1),
                    "mem_pct": round(proc.info.get("memory_percent") or 0, 1),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    result_lines = [
        f"CPU: {cpu_percent:.1f}%",
        f"RAM: {ram.used / 1e9:.1f}GB / {ram.total / 1e9:.1f}GB ({ram.percent:.1f}%)",
        f"Disk (/): {disk.used / 1e9:.1f}GB / {disk.total / 1e9:.1f}GB ({disk.percent:.1f}%)",
        f"Uptime: {uptime_seconds / 3600:.1f}h",
        "",
        "Top processes (by CPU):",
    ]

    for p in processes:
        result_lines.append(
            f"  PID {p['pid']:6d} | {p['name'][:20]:<20} | CPU {p['cpu_pct']:5.1f}% | MEM {p['mem_pct']:5.1f}%"
        )

    result = "\n".join(result_lines)
    _CACHE["data"] = result
    _CACHE["ts"] = now

    logger.info("system_info_fetched", cpu_percent=cpu_percent, ram_pct=ram.percent)
    return result


TOOL_DEFINITION = {
    "name": "system_info",
    "description": (
        "Get a snapshot of system metrics: CPU usage, RAM, disk space, uptime, "
        "and top processes by CPU. No arguments required. Results cached for 30s."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}
