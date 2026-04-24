"""Telegram Bot API channel.

Posts a MarkdownV2 message to ``chat_id`` via ``sendMessage``.
Errors are swallowed after logging — a failing notification
should never take down the server or block a tool call.
"""

from __future__ import annotations

from typing import Any

import httpx


TELEGRAM_API = "https://api.telegram.org"


# MarkdownV2 reserves these characters; any literal occurrence in
# a message body must be backslash-escaped per the Telegram docs.
_MD_V2_ESCAPE_CHARS = set(r"_*[]()~`>#+-=|{}.!\\")


def escape_markdown_v2(text: str) -> str:
    """Escape all MarkdownV2 reserved characters in ``text``.

    This makes the body safe to embed as a plain string inside an
    otherwise-Markdown message — useful for pretty-printing JSON
    blobs in code fences without having them break the formatting.
    """
    out: list[str] = []
    for ch in text:
        if ch in _MD_V2_ESCAPE_CHARS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def format_message(event: str, level: str, summary: str, fields: dict[str, Any]) -> str:
    """Render an event into a MarkdownV2 Telegram message body."""
    level_icon = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}.get(level, "•")
    # Title line: escape every dynamic segment so MarkdownV2 doesn't
    # choke on stray dots / underscores in event names.
    title = f"{level_icon} *{escape_markdown_v2(event)}*"
    body = escape_markdown_v2(summary)
    lines = [title, body]
    if fields:
        pretty = "\n".join(
            f"{escape_markdown_v2(str(k))}: {escape_markdown_v2(str(v))}"
            for k, v in fields.items()
        )
        # Indent the field block under the summary.
        lines.append("")
        lines.append(pretty)
    return "\n".join(lines)


def send(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """POST a MarkdownV2 message. Returns ``(ok, detail)``.

    ``ok=False`` on any HTTP or transport error; ``detail`` carries
    the short reason string the caller logs. Callers are expected
    to treat failures as non-fatal.
    """
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
    except httpx.RequestError as e:
        return False, f"network: {type(e).__name__}"
    if resp.status_code >= 400:
        # Telegram returns a JSON error description; include the
        # first line for quick diagnosis (e.g. 401 for bad token,
        # 400 for bad chat_id or broken MarkdownV2 escaping).
        detail = (resp.text or "").splitlines()
        first = detail[0] if detail else ""
        return False, f"{resp.status_code}: {first}"
    return True, "ok"
