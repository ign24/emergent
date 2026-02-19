"""Web fetch tool with SSRF prevention and timeout."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from emergent import SafetyViolationError

logger = structlog.get_logger(__name__)

MAX_CONTENT_CHARS = 10_000
DEFAULT_TIMEOUT = 15

# Private/loopback IP ranges — SSRF prevention
_PRIVATE_IP_PATTERNS = [
    re.compile(r)
    for r in [
        r"^127\.",
        r"^10\.",
        r"^192\.168\.",
        r"^172\.(1[6-9]|2[0-9]|3[01])\.",
        r"^169\.254\.",  # link-local
        r"^::1$",  # IPv6 loopback
        r"^fc00:",  # IPv6 unique local
        r"^fe80:",  # IPv6 link-local
    ]
]

_BLOCKED_HOSTNAMES = frozenset(["localhost", "127.0.0.1", "0.0.0.0", "::1"])


def _check_ssrf(url: str) -> None:
    """Block requests to private/loopback addresses."""
    parsed = urlparse(url)
    host = parsed.hostname or ""

    if host.lower() in _BLOCKED_HOSTNAMES:
        logger.warning("ssrf_blocked", url=url, host=host)
        raise SafetyViolationError(f"SSRF_BLOCKED: '{host}' is a loopback/private address")

    for pattern in _PRIVATE_IP_PATTERNS:
        if pattern.match(host):
            logger.warning("ssrf_blocked", url=url, host=host)
            raise SafetyViolationError(f"SSRF_BLOCKED: '{host}' is a private IP address")


async def web_fetch(tool_input: dict[str, Any]) -> str:
    """Fetch content from a public URL."""
    url = str(tool_input.get("url", "")).strip()
    max_chars = int(tool_input.get("max_chars", MAX_CONTENT_CHARS))
    max_chars = min(max_chars, MAX_CONTENT_CHARS)

    if not url:
        return "Error: url is required"

    # Upgrade http to https
    if url.startswith("http://"):
        url = "https://" + url[7:]

    if not url.startswith("https://"):
        return "Error: only https:// URLs are supported"

    _check_ssrf(url)

    log = logger.bind(url=url)
    log.info("web_fetch_start")

    headers = {
        "User-Agent": "Emergent-Agent/0.1 (autonomous agent; read-only)",
        "Accept": "text/html,text/plain,application/json",
    }

    retries = 0
    max_retries = 1

    while retries <= max_retries:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
                response = await client.get(url, headers=headers)

            if response.status_code >= 500 and retries < max_retries:
                retries += 1
                log.warning("web_fetch_5xx_retry", status=response.status_code, retry=retries)
                continue

            if response.status_code >= 400:
                return f"Error: HTTP {response.status_code} from {url}"

            content = response.text
            truncated = False
            if len(content) > max_chars:
                content = content[:max_chars] + "\n[... content truncated]"
                truncated = True

            log.info(
                "web_fetch_done",
                status=response.status_code,
                content_len=len(content),
                truncated=truncated,
            )
            return content

        except httpx.TimeoutException:
            if retries < max_retries:
                retries += 1
                log.warning("web_fetch_timeout_retry", retry=retries)
                continue
            return f"Error: request to {url} timed out after {DEFAULT_TIMEOUT}s"
        except httpx.RequestError as e:
            return f"Error: request failed: {e}"

    return "Error: max retries exceeded"


TOOL_DEFINITION = {
    "name": "web_fetch",
    "description": (
        "Fetch content from a public HTTPS URL. Returns text content, truncated at 10,000 chars. "
        "Timeout: 15s. Private/local IPs are blocked (SSRF prevention). "
        "One retry on timeout or 5xx errors."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Public HTTPS URL to fetch",
                "format": "uri",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max characters to return. Default 10000.",
                "default": 10000,
            },
        },
        "required": ["url"],
    },
}
