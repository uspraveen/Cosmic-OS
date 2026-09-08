"""TOTP code generation for vault entries.

The 2FA seed stays encrypted at rest; codes are generated on demand and are
short-lived by construction (30s window). If pyotp is not installed the
feature degrades gracefully — entries without a seed simply have no codes.
"""

from __future__ import annotations

import time

try:
    import pyotp
except ImportError:  # pragma: no cover - handled at call sites
    pyotp = None


def totp_available() -> bool:
    return pyotp is not None


def generate_totp_code(seed: str) -> tuple[str, int] | None:
    """Return (code, seconds_remaining) for a base32 seed, or None."""
    cleaned = str(seed or "").strip().replace(" ", "")
    if not cleaned or pyotp is None:
        return None
    totp = pyotp.TOTP(cleaned)
    return totp.now(), totp.interval - (int(time.time()) % totp.interval)
