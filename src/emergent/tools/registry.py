"""Tool Registry — ToolDefinition, ToolRegistry, SafetyTier."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

from emergent import SafetyViolationError, ToolExecutionError

logger = structlog.get_logger(__name__)


class SafetyTier(Enum):
    TIER_1_AUTO = "auto"  # Read-only: auto-execute
    TIER_2_CONFIRM = "confirm"  # Write/execute: require confirmation
    TIER_3_BLOCKED = "blocked"  # Destructive: always block


class ExecutionContext(Enum):
    USER_SESSION = "user_session"
    CRON_HEADLESS = "cron_headless"


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[str]]
    safety_tier: SafetyTier
    timeout: int = 30


# ---------------------------------------------------------------------------
# Safety Classifier — DETERMINISTIC, NEVER an LLM call
# ---------------------------------------------------------------------------

# Ordered: check TIER_3 first (most restrictive), then TIER_1 allowlist, else TIER_2

_TIER3_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        # Destructive remove
        r"rm\s+-[rf]*r[rf]*\s",
        r"rm\s+--recursive",
        # Privilege escalation
        r"\bsudo\b",
        r"\bsu\s+-",
        r"\bdoas\b",
        # Pipe to shell (code execution)
        r"curl[^|]*\|[^|]*\b(bash|sh|zsh|fish|python|perl|ruby)\b",
        r"wget[^|]*\|[^|]*\b(bash|sh|zsh|fish|python|perl|ruby)\b",
        r"\|[^|]*\b(bash|sh|zsh|fish)\b\s*$",
        # Subshell / command substitution with dangerous commands
        r"\$\([^)]*\brm\b",
        r"\$\([^)]*\bkill\b",
        r"`[^`]*\brm\b",
        # Semicolon/pipe chains with destructive commands
        r"[;&|]\s*rm\s+",
        r"[;&|]\s*sudo\b",
        r"[;&|]\s*mkfs\b",
        # Write to critical system paths
        r">\s*/etc/",
        r">\s*/dev/(sda|hda|nvme|sd[a-z])",
        r">\s*/boot/",
        r">>\s*/etc/passwd",
        r">>\s*/etc/shadow",
        r">>\s*/etc/sudoers",
        # Fork bomb
        r":\s*\(\s*\)\s*\{",
        r"while\s+true\s*;\s*do\s+.*fork",
        # Direct device/disk operations
        r"\bdd\s+if=/dev/zero",
        r"\bdd\s+if=/dev/urandom.*of=/dev/",
        r"\bmkfs\b",
        r"\bfdisk\b",
        r"\bparted\b",
        # Permissions on root or critical paths
        r"chmod\s+[0-7]*[02467][0-7]*\s+/",
        r"chmod\s+777\s+/(etc|bin|sbin|usr|boot)",
        # Network exfiltration pipe
        r"\bnc\s.*\|\s*(bash|sh)",
        # Sensitive files (read/write)
        r"\b(cat|cp|mv|echo)\s+.*/(\.ssh/id_rsa|\.ssh/id_ed25519|\.env)",
        # Base64 decode pipe to shell
        r"base64\s+-d[^|]*\|[^|]*\b(bash|sh)\b",
    ]
]

# TIER_1 allowlist — commands explicitly safe (read-only)
_TIER1_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        r"^ls(\s|$)",
        r"^ls\s+(-[lha]+\s+)*[\w./~\s-]*$",
        r"^cat\s+",
        r"^head\s+",
        r"^tail\s+",
        r"^grep\s+",
        r"^egrep\s+",
        r"^find\s+",
        r"^ps\s",
        r"^ps$",
        r"^pgrep\s+",
        r"^top\s+-b",
        r"^htop\s+-C",
        r"^df\s",
        r"^df$",
        r"^du\s",
        r"^free\s",
        r"^free$",
        r"^uptime$",
        r"^uname\s",
        r"^uname$",
        r"^echo\s+",
        r"^printf\s+",
        r"^date$",
        r"^date\s",
        r"^whoami$",
        r"^id$",
        r"^pwd$",
        r"^env$",
        r"^printenv\s",
        r"^which\s+",
        r"^type\s+",
        r"^wc\s+",
        r"^sort\s+",
        r"^uniq\s+",
        r"^cut\s+",
        r"^awk\s+",
        r"^sed\s+-n\s+",  # sed read-only (-n without -i)
        r"^diff\s+",
        r"^git\s+(status|log|diff|show|branch|remote|fetch|stash\s+list)",
        r"^docker\s+(ps|images|logs|inspect|stats|info|version)",
        r"^docker-compose\s+(ps|logs)",
        r"^systemctl\s+(status|list-units|is-active|is-enabled)",
        r"^journalctl\s+",
        r"^netstat\s+",
        r"^ss\s+",
        r"^ip\s+(addr|route|link)\s",
        r"^ifconfig$",
        r"^ping\s+",
        r"^nslookup\s+",
        r"^dig\s+",
        r"^curl\s+-[^|]*$",  # curl without pipe
        r"^wget\s+-q[^|]*$",  # wget without pipe
        r"^python3?\s+-c\s+.*(print|import\s+sys)",
        r"^pip\s+(list|show|freeze)",
        r"^pip3\s+(list|show|freeze)",
        r"^uv\s+(run|pip\s+list)",
        r"^npm\s+(list|info|outdated)",
        r"^node\s+--version",
        r"^(python3?|pip3?|node|npm|git|docker)\s+--version",
    ]
]

# Patterns that make any command TIER_2 (write but not destructive)
_TIER2_SIGNALS: list[re.Pattern[str]] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        r"\bkill\b",
        r"\bpkill\b",
        r"\bkillall\b",
        r"\brm\b",  # rm without -rf (would be TIER_3 above)
        r"\bmv\b",
        r"\bcp\b.*-[rf]",
        r"\bmkdir\b",
        r"\btouch\b",
        r"\bchmod\b",
        r"\bchown\b",
        r"\bsystemctl\s+(start|stop|restart|enable|disable|reload)",
        r"\bdocker\s+(start|stop|restart|rm|rmi|pull|run|exec)",
        r"\bdocker-compose\s+(up|down|restart|stop|start)",
        r"\bpip\s+install\b",
        r"\bpip3\s+install\b",
        r"\buv\s+add\b",
        r"\bnpm\s+install\b",
        r"\bapt(-get)?\s+(install|remove|purge|upgrade)\b",
        r"\byum\s+(install|remove)\b",
        r"\bsnap\s+(install|remove)\b",
        r"\bgit\s+(commit|push|pull|checkout|reset|merge|rebase|tag)\b",
        r"\bcrontab\b",
        r"\bscreen\b",
        r"\btmux\b",
    ]
]


def classify_command(cmd: str) -> SafetyTier:
    """
    Classify a shell command into a safety tier.

    Algorithm:
        1. Check TIER_3 patterns (most restrictive) — any match → TIER_3
        2. Check TIER_1 allowlist — full match required
        3. Check TIER_2 signals — any match → TIER_2
        4. DEFAULT: TIER_2 (prefer over-blocking to under-blocking)
    """
    cmd = cmd.strip()
    if not cmd:
        return SafetyTier.TIER_2_CONFIRM

    # 1. TIER_3 check (most restrictive)
    for pattern in _TIER3_PATTERNS:
        if pattern.search(cmd):
            logger.warning(
                "tier3_pattern_matched", command_preview=cmd[:50], pattern=pattern.pattern
            )
            return SafetyTier.TIER_3_BLOCKED

    # 2. TIER_1 allowlist — check if any TIER_1 pattern matches start of command
    for pattern in _TIER1_PATTERNS:
        if pattern.match(cmd):
            # Double-check: make sure no TIER_2 signal in the full command
            has_tier2_signal = any(p.search(cmd) for p in _TIER2_SIGNALS)
            if not has_tier2_signal:
                return SafetyTier.TIER_1_AUTO
            break

    # 3. TIER_2 signals
    for pattern in _TIER2_SIGNALS:
        if pattern.search(cmd):
            return SafetyTier.TIER_2_CONFIRM

    # 4. Default: TIER_2 (safe default — prefer over-blocking)
    return SafetyTier.TIER_2_CONFIRM


class ToolRegistry:
    """Registry for all agent tools."""

    def __init__(self, execution_context: ExecutionContext = ExecutionContext.USER_SESSION) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._execution_context = execution_context

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return tool schemas for the Anthropic API."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def classify(self, tool_name: str, tool_input: dict[str, Any]) -> SafetyTier:
        """Classify a tool call into a safety tier."""
        tool = self._tools.get(tool_name)
        if tool is None:
            logger.warning("unknown_tool_classified_tier3", tool_name=tool_name)
            return SafetyTier.TIER_3_BLOCKED

        # Shell tool: dynamic classification based on command content
        if tool_name == "shell_execute":
            command = str(tool_input.get("command", ""))
            tier = classify_command(command)

            # In headless context: TIER_2 → TIER_3 (block)
            if (
                tier == SafetyTier.TIER_2_CONFIRM
                and self._execution_context == ExecutionContext.CRON_HEADLESS
            ):
                logger.warning(
                    "headless_tier2_blocked",
                    tool_name=tool_name,
                    command_preview=command[:50],
                )
                return SafetyTier.TIER_3_BLOCKED

            return tier

        # file_write: always TIER_2 (overwrite check is handled in the tool itself)
        if tool_name == "file_write":
            if self._execution_context == ExecutionContext.CRON_HEADLESS:
                return SafetyTier.TIER_3_BLOCKED
            return SafetyTier.TIER_2_CONFIRM

        # cron_schedule: create/delete → TIER_2; list → TIER_1
        if tool_name == "cron_schedule":
            action = str(tool_input.get("action", ""))
            if action == "list":
                return SafetyTier.TIER_1_AUTO
            if self._execution_context == ExecutionContext.CRON_HEADLESS:
                return SafetyTier.TIER_3_BLOCKED
            return SafetyTier.TIER_2_CONFIRM

        # All other tools use their registered default tier
        return tool.safety_tier

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute a tool by name."""
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolExecutionError(f"Unknown tool: {tool_name}")

        try:
            return await tool.handler(tool_input)
        except SafetyViolationError:
            raise
        except Exception as e:
            raise ToolExecutionError(f"Tool '{tool_name}' failed: {e}") from e
