"""Tool factory — create_registry() assembles all tools."""

from __future__ import annotations

from emergent.tools import files, shell, system_info, web
from emergent.tools.registry import (
    ExecutionContext,
    SafetyTier,
    ToolDefinition,
    ToolRegistry,
)


def create_registry(
    execution_context: ExecutionContext = ExecutionContext.USER_SESSION,
) -> ToolRegistry:
    """Assemble the tool registry with all enabled tools."""
    registry = ToolRegistry(execution_context=execution_context)

    # shell_execute — dynamic tier classification
    registry.register(
        ToolDefinition(
            name="shell_execute",
            description=shell.TOOL_DEFINITION["description"],
            input_schema=shell.TOOL_DEFINITION["input_schema"],
            handler=shell.shell_execute,
            safety_tier=SafetyTier.TIER_1_AUTO,  # overridden dynamically by classify()
        )
    )

    # file_read — TIER_1
    registry.register(
        ToolDefinition(
            name="file_read",
            description=files.FILE_READ_DEFINITION["description"],
            input_schema=files.FILE_READ_DEFINITION["input_schema"],
            handler=files.file_read,
            safety_tier=SafetyTier.TIER_1_AUTO,
        )
    )

    # file_write — TIER_2
    registry.register(
        ToolDefinition(
            name="file_write",
            description=files.FILE_WRITE_DEFINITION["description"],
            input_schema=files.FILE_WRITE_DEFINITION["input_schema"],
            handler=files.file_write,
            safety_tier=SafetyTier.TIER_2_CONFIRM,
        )
    )

    # web_fetch — TIER_1
    registry.register(
        ToolDefinition(
            name="web_fetch",
            description=web.TOOL_DEFINITION["description"],
            input_schema=web.TOOL_DEFINITION["input_schema"],
            handler=web.web_fetch,
            safety_tier=SafetyTier.TIER_1_AUTO,
        )
    )

    # system_info — TIER_1
    registry.register(
        ToolDefinition(
            name="system_info",
            description=system_info.TOOL_DEFINITION["description"],
            input_schema=system_info.TOOL_DEFINITION["input_schema"],
            handler=system_info.system_info,
            safety_tier=SafetyTier.TIER_1_AUTO,
        )
    )

    return registry


__all__ = [
    "create_registry",
    "ExecutionContext",
    "SafetyTier",
    "ToolDefinition",
    "ToolRegistry",
]
