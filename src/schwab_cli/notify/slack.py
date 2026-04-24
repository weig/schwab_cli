"""Slack channel — placeholder until Phase 2b.

The real implementation will accept a webhook URL and POST a
mrkdwn message. For now, any attempt to use the Slack channel
raises :class:`SlackNotYetSupported` so callers fail loudly
instead of silently dropping notifications.
"""

from __future__ import annotations


class SlackNotYetSupported(NotImplementedError):
    """Raised when code tries to actually send via Slack."""


def send(**_kwargs: object) -> tuple[bool, str]:
    raise SlackNotYetSupported(
        "Slack channel not yet supported — see docs/plan/mcp-service.md (Phase 2b). "
        "Use Telegram for MVP notifications."
    )
