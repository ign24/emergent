"""File tools, sandboxed to $HOME."""

from __future__ import annotations

import re
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from emergent import SafetyViolationError

logger = structlog.get_logger(__name__)

SANDBOX_ROOT = Path.home()
MAX_READ_CHARS = 10_000
MAX_WRITE_BYTES = 1_024 * 1_024  # 1MB

# Sensitive paths that should never be read
_SENSITIVE_PATTERNS = [
    ".env",
    "secrets",
    "/etc/shadow",
    "/etc/passwd",
    "/etc/sudoers",
    ".ssh/id_rsa",
    ".ssh/id_ed25519",
    ".ssh/id_ecdsa",
    ".ssh/id_dsa",
    ".ssh/authorized_keys",
    "credentials",
    "config/database",
]

_SENSITIVE_EXTENSIONS = [".pem", ".key", ".p12", ".pfx"]


def _resolve_path(path_str: str) -> Path:
    """Resolve path relative to $HOME, blocking traversal and sensitive files."""
    if path_str.startswith("/"):
        resolved = Path(path_str).resolve()
    else:
        resolved = (SANDBOX_ROOT / path_str).resolve()

    # Block path traversal
    try:
        resolved.relative_to(SANDBOX_ROOT)
    except ValueError:
        raise SafetyViolationError(f"OUTSIDE_SANDBOX: path '{path_str}' resolves outside $HOME")

    # Block '..' traversal attempts
    if ".." in Path(path_str).parts:
        raise SafetyViolationError(f"PATH_TRAVERSAL: '..' not allowed in path '{path_str}'")

    # Check sensitive paths
    path_lower = str(resolved).lower()
    rel_str = str(resolved.relative_to(SANDBOX_ROOT)).lower()

    for sensitive in _SENSITIVE_PATTERNS:
        if sensitive.lower() in path_lower:
            logger.warning("sensitive_path_blocked", path=str(resolved), pattern=sensitive)
            raise SafetyViolationError(f"SENSITIVE_PATH: '{resolved.name}' is a sensitive file")

    if resolved.suffix.lower() in _SENSITIVE_EXTENSIONS:
        raise SafetyViolationError(f"SENSITIVE_PATH: extension '{resolved.suffix}' is blocked")

    return resolved


async def file_read(tool_input: dict[str, Any]) -> str:
    """Read a file from $HOME sandbox."""
    path_str = str(tool_input.get("path", ""))
    max_chars = int(tool_input.get("max_chars", MAX_READ_CHARS))
    max_chars = min(max_chars, MAX_READ_CHARS)

    if not path_str:
        return "Error: path is required"

    resolved = _resolve_path(path_str)

    if not resolved.exists():
        return f"Error: FILE_NOT_FOUND: '{resolved}' does not exist"

    if not resolved.is_file():
        return f"Error: '{resolved}' is not a file"

    try:
        size_bytes = resolved.stat().st_size
        content = resolved.read_text(errors="replace")
    except PermissionError:
        raise SafetyViolationError(f"PERMISSION_DENIED: cannot read '{resolved}'")

    truncated = False
    if len(content) > max_chars:
        content = content[:max_chars] + "\n[... file truncated]"
        truncated = True

    logger.info("file_read", path=str(resolved), size_bytes=size_bytes, truncated=truncated)
    return content


async def file_write(tool_input: dict[str, Any]) -> str:
    """Write/create a file in $HOME sandbox."""
    path_str = str(tool_input.get("path", ""))
    content = str(tool_input.get("content", ""))
    mode = str(tool_input.get("mode", "create"))

    if mode not in ("create", "overwrite", "append"):
        return "Error: mode must be 'create', 'overwrite', or 'append'"

    if not path_str:
        return "Error: path is required"

    if len(content.encode()) > MAX_WRITE_BYTES:
        return f"Error: content exceeds max size of {MAX_WRITE_BYTES // 1024}KB"

    resolved = _resolve_path(path_str)

    # Create parent directories if needed
    resolved.parent.mkdir(parents=True, exist_ok=True)

    try:
        if mode == "create":
            try:
                # Atomic create: fails if file exists (no TOCTOU race)
                with open(resolved, "x", encoding="utf-8") as f:
                    f.write(content)
            except FileExistsError:
                return "Error: file already exists. Use mode='overwrite' to replace it."
            action = "created"
        elif mode == "overwrite":
            resolved.write_text(content)
            action = "overwritten"
        elif mode == "append":
            with open(resolved, "a") as f:
                f.write(content)
            action = "appended"
        else:
            action = "unknown"
    except PermissionError:
        raise SafetyViolationError(f"PERMISSION_DENIED: cannot write '{resolved}'")

    logger.info("file_write", path=str(resolved), mode=mode, bytes_written=len(content.encode()))
    return f"File {action}: {resolved} ({len(content.encode())} bytes)"


FILE_READ_DEFINITION = {
    "name": "file_read",
    "description": (
        "Read the content of a file. Path is relative to $HOME. "
        "Sensitive files (.env, .ssh keys, secrets) are blocked. "
        "Output is truncated at 10,000 chars."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to $HOME (e.g., 'Documents/notes.txt')",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max characters to return. Default 10000.",
                "default": 10000,
            },
        },
        "required": ["path"],
    },
}

FILE_WRITE_DEFINITION = {
    "name": "file_write",
    "description": (
        "Create or write a file in $HOME. Requires user confirmation if file already exists. "
        "Mode: 'create' (fails if exists), 'overwrite' (replaces), 'append' (adds to end). "
        "Max content size: 1MB."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to $HOME",
            },
            "content": {
                "type": "string",
                "description": "Content to write",
            },
            "mode": {
                "type": "string",
                "enum": ["create", "overwrite", "append"],
                "description": "Write mode. Default: 'create'",
                "default": "create",
            },
        },
        "required": ["path", "content"],
    },
}


# ---------------------------------------------------------------------------
# list_directory
# ---------------------------------------------------------------------------


def _format_size(size: int) -> str:
    """Format byte size as human-readable string."""
    fsize = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if fsize < 1024:
            return f"{fsize:.0f}{unit}" if unit == "B" else f"{fsize:.1f}{unit}"
        fsize /= 1024
    return f"{fsize:.1f}TB"


async def list_directory(tool_input: dict[str, Any]) -> str:
    """List contents of a directory."""
    path_str = str(tool_input.get("path", "."))
    show_hidden = bool(tool_input.get("show_hidden", False))

    resolved = _resolve_path(path_str)

    if not resolved.exists():
        return f"Error: directory '{resolved}' does not exist"
    if not resolved.is_dir():
        return f"Error: '{resolved}' is not a directory"

    try:
        entries = list(resolved.iterdir())
    except PermissionError as err:
        raise SafetyViolationError(f"PERMISSION_DENIED: cannot list '{resolved}'") from err

    if not show_hidden:
        entries = [e for e in entries if not e.name.startswith(".")]

    dirs = sorted([e for e in entries if e.is_dir()], key=lambda p: p.name.lower())
    files = sorted([e for e in entries if not e.is_dir()], key=lambda p: p.name.lower())

    lines: list[str] = []
    for d in dirs:
        lines.append(f"[DIR]  {d.name}/")
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        lines.append(f"[FILE] {f.name} ({_format_size(size)})")

    if not lines:
        return f"Directory '{path_str}' is empty"

    logger.info("list_directory", path=str(resolved), entries=len(lines))
    return "\n".join(lines)


LIST_DIRECTORY_DEFINITION = {
    "name": "list_directory",
    "description": (
        "List contents of a directory in $HOME. "
        "Shows directories first, then files with sizes. "
        "Hidden files (dotfiles) are excluded by default."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to $HOME. Default: $HOME",
                "default": ".",
            },
            "show_hidden": {
                "type": "boolean",
                "description": "Include hidden files/directories (dotfiles). Default: false",
                "default": False,
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# directory_tree
# ---------------------------------------------------------------------------

_MAX_TREE_ENTRIES = 200


def _build_tree(path: Path, prefix: str, depth: int, max_depth: int, lines: list[str]) -> None:
    """Recursively build tree lines."""
    if depth > max_depth or len(lines) >= _MAX_TREE_ENTRIES:
        return

    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return

    # Filter hidden
    entries = [e for e in entries if not e.name.startswith(".")]

    for i, entry in enumerate(entries):
        if len(lines) >= _MAX_TREE_ENTRIES:
            lines.append(f"{prefix}... (truncated at {_MAX_TREE_ENTRIES} entries)")
            return

        is_last = i == len(entries) - 1
        connector = "└── " if is_last else "├── "
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"{prefix}{connector}{entry.name}{suffix}")

        if entry.is_dir() and depth < max_depth:
            extension = "    " if is_last else "│   "
            _build_tree(entry, prefix + extension, depth + 1, max_depth, lines)


async def directory_tree(tool_input: dict[str, Any]) -> str:
    """Show recursive directory tree."""
    path_str = str(tool_input.get("path", "."))
    max_depth = int(tool_input.get("max_depth", 3))
    max_depth = max(1, min(max_depth, 5))

    resolved = _resolve_path(path_str)

    if not resolved.exists():
        return f"Error: directory '{resolved}' does not exist"
    if not resolved.is_dir():
        return f"Error: '{resolved}' is not a directory"

    lines: list[str] = [f"{resolved.name}/"]
    _build_tree(resolved, "", 1, max_depth, lines)

    logger.info("directory_tree", path=str(resolved), entries=len(lines))
    return "\n".join(lines)


DIRECTORY_TREE_DEFINITION = {
    "name": "directory_tree",
    "description": (
        "Show a recursive directory tree with configurable depth. "
        "Max depth is 5, max entries is 200. Hidden files are excluded."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to $HOME. Default: $HOME",
                "default": ".",
            },
            "max_depth": {
                "type": "integer",
                "description": "Maximum recursion depth (1-5). Default: 3",
                "default": 3,
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------


async def search_files(tool_input: dict[str, Any]) -> str:
    """Search for files by glob pattern."""
    path_str = str(tool_input.get("path", "."))
    pattern = str(tool_input.get("pattern", ""))
    max_results = int(tool_input.get("max_results", 20))
    max_results = max(1, min(max_results, 50))

    if not pattern:
        return "Error: pattern is required"

    resolved = _resolve_path(path_str)

    if not resolved.exists() or not resolved.is_dir():
        return f"Error: directory '{resolved}' does not exist"

    results: list[str] = []
    try:
        for match in resolved.rglob(pattern):
            if len(results) >= max_results:
                break
            # Skip hidden directories/files in path
            if any(part.startswith(".") for part in match.relative_to(resolved).parts):
                continue
            try:
                rel = match.relative_to(SANDBOX_ROOT)
            except ValueError:
                continue
            suffix = "/" if match.is_dir() else ""
            results.append(f"{rel}{suffix}")
    except PermissionError:
        pass

    if not results:
        return f"No files matching '{pattern}' found in '{path_str}'"

    header = f"Found {len(results)} result(s) for '{pattern}':\n"
    logger.info("search_files", path=str(resolved), pattern=pattern, results=len(results))
    return header + "\n".join(results)


SEARCH_FILES_DEFINITION = {
    "name": "search_files",
    "description": (
        "Search for files matching a glob pattern recursively. "
        "Pattern examples: '*.py', '*.txt', 'config.*'. "
        "Returns paths relative to $HOME."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Starting directory relative to $HOME. Default: $HOME",
                "default": ".",
            },
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match (e.g., '*.py', 'config.*')",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum results to return (1-50). Default: 20",
                "default": 20,
            },
        },
        "required": ["pattern"],
    },
}


# ---------------------------------------------------------------------------
# search_in_files
# ---------------------------------------------------------------------------

_MAX_LINE_DISPLAY = 200


def _is_binary(path: Path) -> bool:
    """Heuristic: file is binary if first 8KB contain null bytes."""
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" in chunk
    except OSError:
        return True


async def search_in_files(tool_input: dict[str, Any]) -> str:
    """Search for text/regex inside files."""
    path_str = str(tool_input.get("path", "."))
    query = str(tool_input.get("query", ""))
    glob_pattern = str(tool_input.get("glob", "*"))
    max_results = int(tool_input.get("max_results", 20))
    max_results = max(1, min(max_results, 50))

    if not query:
        return "Error: query is required"

    resolved = _resolve_path(path_str)

    if not resolved.exists() or not resolved.is_dir():
        return f"Error: directory '{resolved}' does not exist"

    # Try to compile as regex; fall back to literal
    try:
        regex = re.compile(query)
    except re.error:
        regex = re.compile(re.escape(query))

    matches: list[str] = []
    try:
        for filepath in resolved.rglob(glob_pattern):
            if len(matches) >= max_results:
                break
            if not filepath.is_file():
                continue
            # Skip hidden
            if any(part.startswith(".") for part in filepath.relative_to(resolved).parts):
                continue
            if _is_binary(filepath):
                continue

            try:
                rel = filepath.relative_to(SANDBOX_ROOT)
                content = filepath.read_text(errors="replace")
            except (OSError, ValueError):
                continue

            for line_no, line in enumerate(content.splitlines(), 1):
                if len(matches) >= max_results:
                    break
                if regex.search(line):
                    display_line = line.strip()
                    if len(display_line) > _MAX_LINE_DISPLAY:
                        display_line = display_line[:_MAX_LINE_DISPLAY] + "..."
                    matches.append(f"{rel}:{line_no}: {display_line}")
    except PermissionError:
        pass

    if not matches:
        return f"No matches for '{query}' in '{path_str}'"

    header = f"Found {len(matches)} match(es) for '{query}':\n"
    logger.info("search_in_files", path=str(resolved), query=query, matches=len(matches))
    return header + "\n".join(matches)


SEARCH_IN_FILES_DEFINITION = {
    "name": "search_in_files",
    "description": (
        "Search for text or regex patterns inside files (like grep). "
        "Returns matching lines with file path and line number. "
        "Skips binary files automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Starting directory relative to $HOME. Default: $HOME",
                "default": ".",
            },
            "query": {
                "type": "string",
                "description": "Text or regex pattern to search for",
            },
            "glob": {
                "type": "string",
                "description": "File glob filter (e.g., '*.py', '*.txt'). Default: '*'",
                "default": "*",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum matches to return (1-50). Default: 20",
                "default": 20,
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# file_info
# ---------------------------------------------------------------------------


async def file_info(tool_input: dict[str, Any]) -> str:
    """Get file/directory metadata."""
    path_str = str(tool_input.get("path", ""))

    if not path_str:
        return "Error: path is required"

    resolved = _resolve_path(path_str)

    if not resolved.exists():
        return f"Error: '{resolved}' does not exist"

    try:
        st = resolved.stat()
    except PermissionError as err:
        raise SafetyViolationError(f"PERMISSION_DENIED: cannot stat '{resolved}'") from err

    if resolved.is_file():
        file_type = "file"
    elif resolved.is_dir():
        file_type = "directory"
    elif resolved.is_symlink():
        file_type = "symlink"
    else:
        file_type = "other"
    perms = stat.filemode(st.st_mode)
    _ts_fmt = "%Y-%m-%d %H:%M:%S UTC"
    modified = datetime.fromtimestamp(st.st_mtime, tz=UTC).strftime(_ts_fmt)
    created = datetime.fromtimestamp(st.st_ctime, tz=UTC).strftime(_ts_fmt)

    info_lines = [
        f"Path: {resolved}",
        f"Type: {file_type}",
        f"Size: {_format_size(st.st_size)}",
        f"Permissions: {perms}",
        f"Modified: {modified}",
        f"Created: {created}",
    ]

    logger.info("file_info", path=str(resolved))
    return "\n".join(info_lines)


FILE_INFO_DEFINITION = {
    "name": "file_info",
    "description": (
        "Get metadata about a file or directory: type, size, permissions, "
        "modification and creation timestamps."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File or directory path relative to $HOME",
            },
        },
        "required": ["path"],
    },
}


# ---------------------------------------------------------------------------
# file_move
# ---------------------------------------------------------------------------


async def file_move(tool_input: dict[str, Any]) -> str:
    """Move or rename a file/directory."""
    source_str = str(tool_input.get("source", ""))
    dest_str = str(tool_input.get("destination", ""))

    if not source_str:
        return "Error: source is required"
    if not dest_str:
        return "Error: destination is required"

    source = _resolve_path(source_str)
    destination = _resolve_path(dest_str)

    if not source.exists():
        return f"Error: source '{source}' does not exist"

    if destination.exists():
        return f"Error: destination '{destination}' already exists"

    # Create parent directories if needed
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        shutil.move(str(source), str(destination))
    except PermissionError as err:
        raise SafetyViolationError(f"PERMISSION_DENIED: cannot move '{source}'") from err

    logger.info("file_move", source=str(source), destination=str(destination))
    return f"Moved: {source} → {destination}"


FILE_MOVE_DEFINITION = {
    "name": "file_move",
    "description": (
        "Move or rename a file or directory within $HOME. "
        "Both source and destination must be inside $HOME. "
        "Fails if destination already exists."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Source path relative to $HOME",
            },
            "destination": {
                "type": "string",
                "description": "Destination path relative to $HOME",
            },
        },
        "required": ["source", "destination"],
    },
}


# ---------------------------------------------------------------------------
# file_delete
# ---------------------------------------------------------------------------


async def file_delete(tool_input: dict[str, Any]) -> str:
    """Delete a file or directory."""
    path_str = str(tool_input.get("path", ""))
    recursive = bool(tool_input.get("recursive", False))

    if not path_str:
        return "Error: path is required"

    resolved = _resolve_path(path_str)

    if not resolved.exists():
        return f"Error: '{resolved}' does not exist"

    # Protect sandbox root itself
    if resolved == SANDBOX_ROOT:
        raise SafetyViolationError("PROTECTED_PATH: cannot delete sandbox root")

    try:
        if resolved.is_file() or resolved.is_symlink():
            resolved.unlink()
            logger.info("file_delete", path=str(resolved), type="file")
            return f"Deleted file: {resolved}"
        elif resolved.is_dir():
            if not recursive:
                # Check if empty
                if any(resolved.iterdir()):
                    return (
                        f"Error: directory '{resolved}' is not empty. "
                        "Use recursive=true to delete non-empty directories."
                    )
                resolved.rmdir()
            else:
                shutil.rmtree(str(resolved))
            logger.info("file_delete", path=str(resolved), type="directory", recursive=recursive)
            return f"Deleted directory: {resolved}"
        else:
            return f"Error: '{resolved}' is not a regular file or directory"
    except PermissionError as err:
        raise SafetyViolationError(f"PERMISSION_DENIED: cannot delete '{resolved}'") from err


FILE_DELETE_DEFINITION = {
    "name": "file_delete",
    "description": (
        "Delete a file or directory within $HOME. "
        "Non-empty directories require recursive=true. "
        "Cannot delete the sandbox root ($HOME)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to delete, relative to $HOME",
            },
            "recursive": {
                "type": "boolean",
                "description": "Delete non-empty directories recursively. Default: false",
                "default": False,
            },
        },
        "required": ["path"],
    },
}
