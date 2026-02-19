"""System prompt builder for the agent."""

from __future__ import annotations

from datetime import datetime

DEFAULT_SYSTEM_PROMPT = """\
Sos Emergent, un agente autónomo personal corriendo en el sistema local de tu dueño.
Tenés acceso a herramientas para ejecutar comandos de shell, leer y escribir archivos,
obtener información del sistema, buscar en la web, y gestionar tu memoria.

Principios:
- Sé conciso y directo. El usuario te habla desde Telegram.
- Antes de ejecutar comandos destructivos, explicá qué vas a hacer.
- Usa tu memoria para recordar preferencias y contexto previo.
- Si no podés hacer algo por seguridad, explicá por qué claramente.
- Respondé en el mismo idioma que el usuario.\
"""


def build_system_prompt(
    base_prompt: str,
    user_profile: str | None = None,
    semantic_memories: list[str] | None = None,
    session_summary: str | None = None,
) -> str:
    """Build the full system prompt injecting memory context."""
    parts: list[str] = [base_prompt or DEFAULT_SYSTEM_PROMPT]

    # Current date/time context
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    parts.append(f"\nFecha y hora actual: {now}")

    if user_profile:
        parts.append(f"\n## Perfil del usuario\n{user_profile}")

    if semantic_memories:
        memories_text = "\n".join(f"- {m}" for m in semantic_memories)
        parts.append(f"\n## Memorias relevantes\n{memories_text}")

    if session_summary:
        parts.append(f"\n## Resumen de sesión anterior\n{session_summary}")

    return "\n".join(parts)
