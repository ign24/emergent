"""Startup banner for Emergent."""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

_LOGO = """\
 ███████╗███╗   ███╗███████╗██████╗  ██████╗ ███████╗███╗   ██╗████████╗✦ ✧
 ██╔════╝████╗ ████║██╔════╝██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
 █████╗  ██╔████╔██║█████╗  ██████╔╝██║  ███╗█████╗  ██╔██╗ ██║   ██║
 ██╔══╝  ██║╚██╔╝██║██╔══╝  ██╔══██╗██║   ██║██╔══╝  ██║╚██╗██║   ██║
 ███████╗██║ ╚═╝ ██║███████╗██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║
 ╚══════╝╚═╝     ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝\
"""

_ACCENT = "#06B6D4"
_GRADIENT_START = (34, 197, 94)
_GRADIENT_END = (6, 182, 212)
_DIM = "grey50"
_OK = "green"
_RULE = "grey30"
_ERR = "red"


def _hex_gradient(start: tuple[int, int, int], end: tuple[int, int, int], t: float) -> str:
    r = int(start[0] + (end[0] - start[0]) * t)
    g = int(start[1] + (end[1] - start[1]) * t)
    b = int(start[2] + (end[2] - start[2]) * t)
    return f"#{r:02X}{g:02X}{b:02X}"


def _gradient_logo(logo: str) -> Text:
    text = Text(logo)
    colorable = [i for i, ch in enumerate(logo) if not ch.isspace()]
    total = max(len(colorable) - 1, 1)
    for pos, index in enumerate(colorable):
        ratio = pos / total
        text.stylize(_hex_gradient(_GRADIENT_START, _GRADIENT_END, ratio), index, index + 1)
    return text


def print_banner(
    version: str,
    provider: str,
    model: str,
    db_path: str,
    chroma_dir: str,
    allowed_users: int,
    scheduler_jobs: int,
    log_file: str | None = None,
    telegram_enabled: bool = True,
    voice_enabled: bool = False,
) -> None:
    """Print the Emergent startup banner to stdout."""
    console = Console(highlight=False)

    console.print()

    # Logo
    logo_text = _gradient_logo(_LOGO)
    console.print(logo_text)

    console.print()

    # Subtitle row
    console.print(
        f"  [bold white]v{version}[/]  "
        f"[#06B6D4]·[/]  "
        f"[#06B6D4]{provider}[/][white]:{model}[/]  "
        f"[white]·[/]  "
        f"[{_DIM}]local-first autonomous agent[/]"
    )

    console.print()

    # Status indicators
    def _row(label: str, value: str) -> None:
        console.print(f"  [{_OK}]●[/]  [{_DIM}]{label:<14}[/]  [white]{value}[/]")

    _row("SQLite WAL", db_path)
    _row("ChromaDB", chroma_dir)
    _row("Terminal", f"interactive  [{_DIM}]·[/]  session=terminal_session")
    if voice_enabled:
        _row("Voice", "push-to-talk  [grey50]·[/]  session=voice_session")
    if telegram_enabled:
        suffix = "s" if allowed_users != 1 else ""
        _row("Telegram", f"polling  [{_DIM}]·[/]  {allowed_users} user{suffix} authorized")
    _row("Scheduler", f"{scheduler_jobs} jobs loaded")
    if log_file:
        _row("Logs", log_file)

    console.print()
    console.print(
        "  [#22C55E]Tips[/]: "
        "[white]ESC ESC[/] salir  "
        "[#22C55E]/skills[/] modos  "
        "[#06B6D4]/session[/] sesiones"
    )

    console.print()

    # Rule
    console.rule(style=_RULE)

    console.print()


class ConsoleNotifier:
    """Prints brief activity lines to stderr so the terminal stays informative."""

    def __init__(self) -> None:
        self._console = Console(stderr=True, highlight=False)

    def message_received(self, user: str, preview: str, length: int) -> None:
        self._console.print(
            f"  [{_DIM}]←[/] [{_ACCENT}]{user}[/][{_DIM}]:[/] "
            f'[white]"{preview}"[/] [{_DIM}]({length} chars)[/]'
        )

    def message_sent(self, duration_secs: float, tokens: int) -> None:
        self._console.print(
            f"  [{_OK}]→[/] [{_DIM}]{duration_secs:.1f}s[/] "
            f"[{_DIM}]·[/] [{_DIM}]{tokens:,} tokens[/]"
        )

    def error(self, msg: str) -> None:
        self._console.print(f"  [{_ERR}]✗[/] [{_ERR}]error:[/] [{_DIM}]{msg}[/]")
