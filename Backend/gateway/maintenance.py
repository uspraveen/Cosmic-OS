"""Maintenance-mode drain flag, checked on webhook intake paths.

During a planned fleet restart, `cosmic-restart-agent` raises a drain flag
file a few seconds before stopping services. Push-based ingress endpoints
(Gmail Pub/Sub, GitHub, Telegram, WhatsApp bridge, agent-email) then answer
503 + Retry-After instead of accepting work they might not finish -- every
one of those senders retries non-2xx deliveries on their own, so nothing is
lost. Desktop/mobile sends are deliberately NOT drained: they are synchronous
user actions without sender-side retry, and the drain window is only seconds.

The flag lives in /run (tmpfs) so it can never outlive a boot, and the
restart helper clears it once the gateway is healthy again.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DRAIN_FLAG = Path("/run/cosmic-drain")

# Ingress paths with sender-side retry semantics. Exact-match or prefix --
# keep this tight so only auto-retried traffic is ever refused.
DRAIN_RETRY_PREFIXES = (
    "/webhooks/",
    "/internal/channels/",
    "/channels/telegram/webhook",
)

RETRY_AFTER_SECONDS = 60


def drain_flag_path() -> Path:
    return Path(os.getenv("COSMIC_DRAIN_FLAG", str(DEFAULT_DRAIN_FLAG)))


def drain_active() -> bool:
    """True while a graceful restart is draining/restarting the fleet."""
    try:
        return drain_flag_path().exists()
    except OSError:
        return False


def drain_retry_response() -> tuple[int, str, dict[str, str]]:
    """(status, body, headers) for a drained webhook request."""
    headers = {"Retry-After": str(RETRY_AFTER_SECONDS)}
    body = json.dumps({"detail": "cosmic is restarting; retry shortly"})
    return 503, body, headers


def request_should_drain(path: str) -> bool:
    return path.startswith(DRAIN_RETRY_PREFIXES)
