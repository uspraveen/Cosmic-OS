"""Shared helpers for the browser commit-authorization policy.

A "commit" is a control that persists, submits, sends, deletes, orders, or
pays. The browser engine detects them deterministically and holds them; the
orchestrator decides whether the user's own instruction already authorizes the
action, or whether it has to be confirmed on a card. These helpers keep the
action-class key and the human-readable summary identical on both sides.
"""

from __future__ import annotations

from typing import Any


def commit_action_class(commit: dict[str, Any] | None) -> str:
    """Stable class for granting/confirming a commit ("apply", "submit", ...).

    Grants are scoped per task + class, so "apply to 50 jobs" needs one
    confirmation at most and every later application is covered.
    """
    if not isinstance(commit, dict):
        return "submit"
    control = commit.get("control") if isinstance(commit.get("control"), dict) else {}
    matched = control.get("matched") if isinstance(control.get("matched"), list) else []
    for verb in matched:
        text = str(verb or "").strip().lower()
        if text:
            return text.replace(" ", "_")
    return "submit"


def commit_summary(commit: dict[str, Any] | None) -> str:
    """One-line, secret-free summary for logs and the confirmation request."""
    if not isinstance(commit, dict):
        return "a commit action"
    control = commit.get("control") if isinstance(commit.get("control"), dict) else {}
    target = str(commit.get("target") or control.get("name") or "a control").strip()[:120]
    url = str(commit.get("url") or "").strip()
    irreversible = bool(commit.get("irreversible"))
    suffix = " (irreversible)" if irreversible else ""
    return f"{target} on {url}{suffix}" if url else f"{target}{suffix}"
