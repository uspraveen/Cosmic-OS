"""Durable sender/domain prefilter for Gmail triage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .google_gmail_client import extract_domain, extract_email_address


DEFAULT_PREFILTER = {
    "version": 1,
    "blocked_senders": [],
    "blocked_domains": [],
    "notes": [],
}


class SenderPrefilter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps(DEFAULT_PREFILTER, indent=2), encoding="utf-8")

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            payload = dict(DEFAULT_PREFILTER)
        if not isinstance(payload, dict):
            payload = dict(DEFAULT_PREFILTER)
        payload.setdefault("version", 1)
        payload.setdefault("blocked_senders", [])
        payload.setdefault("blocked_domains", [])
        payload.setdefault("notes", [])
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        normalized = {
            "version": 1,
            "blocked_senders": self._normalize_entries(payload.get("blocked_senders")),
            "blocked_domains": self._normalize_entries(payload.get("blocked_domains")),
            "notes": payload.get("notes") if isinstance(payload.get("notes"), list) else [],
        }
        self.path.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")

    def match(self, sender_value: str) -> dict[str, Any] | None:
        email = extract_email_address(sender_value)
        domain = extract_domain(email)
        payload = self.load()
        for entry in payload.get("blocked_senders") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("value") or "").strip().lower() == email:
                return {"type": "sender", **entry}
        for entry in payload.get("blocked_domains") or []:
            if not isinstance(entry, dict):
                continue
            if domain and str(entry.get("value") or "").strip().lower() == domain:
                return {"type": "domain", **entry}
        return None

    def add_sender(self, sender_value: str, *, reason: str, source: str = "llm") -> dict[str, Any]:
        email = extract_email_address(sender_value)
        if not email or "@" not in email:
            raise ValueError("A valid sender email address is required.")
        payload = self.load()
        entries = [item for item in payload.get("blocked_senders") or [] if isinstance(item, dict)]
        existing = next((item for item in entries if str(item.get("value") or "").lower() == email), None)
        now = _now()
        if existing:
            existing["reason"] = reason or existing.get("reason") or ""
            existing["updated_at"] = now
            existing["source"] = source or existing.get("source") or "manual"
        else:
            entries.append(
                {
                    "value": email,
                    "reason": reason,
                    "source": source,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        payload["blocked_senders"] = entries
        self.save(payload)
        return {"type": "sender", "value": email, "reason": reason, "source": source}

    def add_domain(self, domain_value: str, *, reason: str, source: str = "llm") -> dict[str, Any]:
        domain = str(domain_value or "").strip().lower()
        if "@" in domain:
            domain = extract_domain(domain)
        if not domain or "." not in domain:
            raise ValueError("A valid sender domain is required.")
        payload = self.load()
        entries = [item for item in payload.get("blocked_domains") or [] if isinstance(item, dict)]
        existing = next((item for item in entries if str(item.get("value") or "").lower() == domain), None)
        now = _now()
        if existing:
            existing["reason"] = reason or existing.get("reason") or ""
            existing["updated_at"] = now
            existing["source"] = source or existing.get("source") or "manual"
        else:
            entries.append(
                {
                    "value": domain,
                    "reason": reason,
                    "source": source,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        payload["blocked_domains"] = entries
        self.save(payload)
        return {"type": "domain", "value": domain, "reason": reason, "source": source}

    def remove(self, value: str) -> bool:
        target = str(value or "").strip().lower()
        if not target:
            return False
        payload = self.load()
        changed = False
        next_senders = []
        for item in payload.get("blocked_senders") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("value") or "").strip().lower() == target:
                changed = True
                continue
            next_senders.append(item)
        next_domains = []
        for item in payload.get("blocked_domains") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("value") or "").strip().lower() == target:
                changed = True
                continue
            next_domains.append(item)
        payload["blocked_senders"] = next_senders
        payload["blocked_domains"] = next_domains
        if changed:
            self.save(payload)
        return changed

    def _normalize_entries(self, raw: Any) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return entries
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value") or "").strip().lower()
            if not value or value in seen:
                continue
            seen.add(value)
            entries.append({**item, "value": value})
        return entries[-1000:]


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
