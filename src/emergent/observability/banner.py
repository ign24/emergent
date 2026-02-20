"""Startup banner for Emergent."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

_LOGO = """\
 ███████╗███╗   ███╗███████╗██████╗  ██████╗ ███████╗███╗   ██╗████████╗
 ██╔════╝████╗ ████║██╔════╝██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
 █████╗  ██╔████╔██║█████╗  ██████╔╝██║  ███╗█████╗  ██╔██╗ ██║   ██║
 ██╔══╝  ██║╚██╔╝██║██╔══╝  ██╔══██╗██║   ██║██╔══╝  ██║╚██╗██║   ██║
 ███████╗██║ ╚═╝ ██║███████╗██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║
 ╚══════╝╚═╝     ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝\
"""

_ACCENT = "#7C3AED"   # violet
_DIM    = "grey50"
_OK     = "green"
_RULE   = "grey30"


def print_banner(
    version: str,
    model: str,
    db_path: str,
    chroma_dir: str,
    allowed_users: int,
    scheduler_jobs: int,
) -> None:
    """Print the Emergent startup banner to stdout."""
    console = Console(highlight=False)

    console.print()

    # Logo
    logo_text = Text(_LOGO)
    logo_text.stylize(_ACCENT)
    console.print(logo_text)

    console.print()

    # Subtitle row
    console.print(
        f"  [bold white]v{version}[/]  "
        f"[{_DIM}]·[/]  "
        f"[{_DIM}]{model}[/]  "
        f"[{_DIM}]·[/]  "
        f"[{_DIM}]local-first autonomous agent[/]"
    )

    console.print()

    # Status indicators
    def _row(label: str, value: str) -> None:
        console.print(
            f"  [{_OK}]●[/]  [{_DIM}]{label:<14}[/]  [white]{value}[/]"
        )

    _row("SQLite WAL", db_path)
    _row("ChromaDB", chroma_dir)
    _row("Telegram", f"polling  [{_DIM}]·[/]  {allowed_users} user{'s' if allowed_users != 1 else ''} authorized")
    _row("Scheduler", f"{scheduler_jobs} jobs loaded")

    console.print()

    # Rule
    console.rule(style=_RULE)

    console.print()
