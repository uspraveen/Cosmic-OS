"""Durable record of commit labels the deterministic classifier missed.

When the browser model proactively requests authorization for a control the
classifier did not flag, the label is appended here (JSONL). These are the
candidates to fold into the deterministic verb list, so the gate gets tighter
from production misses instead of guesswork. Best-effort by design: a logging
failure never blocks the authorization flow.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def commit_miss_path(sessions_db_path: Path | str) -> Path:
    """Sits beside the gateway's SQLite stores; no schema, append-only."""
    return Path(sessions_db_path).parent / "browser_commit_misses.jsonl"


def record_commit_miss(path: Path, entry: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **entry,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception:
        logger.warning("commit_miss.record_failed", exc_info=True)
        return False
