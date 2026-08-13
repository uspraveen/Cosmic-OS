"""Structured watchpoint retractions for heartbeat notes.

Heartbeat notes restate world facts they do not own. A user correction in chat
or email therefore cannot reach the scratchpad copy unless something other than
the model deletes it. This module is that something: deterministic, lexical,
and fail-closed on ambiguity.

It does *not* infer contradictions from nearby memories. Related retrieval is
how a wrong session summary (coprlab.com had an outage) can agree with a stale
watchpoint and beat a real correction. Only an explicit retraction - structured
`invalidates` phrases, or a tight "not X.com but Y.com" user utterance - may
strike a standing watchpoint.

Beat-log lines are self-observation and are left alone. Rewriting history would
hide what was actually delivered.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import Any

MIN_INVALIDATE_CHARS = 8
SUPERSEDED_PREFIX = "SUPERSEDED:"

# Same family as scratchpad beat-log markers: these lines are what the
# heartbeat did, not facts about the world.
_LOG_LINE_MARKERS = ("suppressed", "delivered", "delivering", "no material change")
_LOG_SECTION_RE = re.compile(
    r"^#{1,6}\s+(suppression log|correction log|beat log)\b",
    re.IGNORECASE,
)

# Hostnames only. Arbitrary "not X but Y" in prose is too easy to misfire on.
_HOST = (
    r"(?:www\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
_NOT_BUT_HOST_RE = re.compile(
    rf"(?:it['’]?s\s+)?not\s+({_HOST})\s+but\s+({_HOST})",
    re.IGNORECASE,
)

_FILE_LOCK = threading.Lock()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def normalize_hostname(value: str) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if text.startswith("www."):
        text = text[4:]
    return text


def sanitize_invalidate_phrases(values: Any) -> list[str]:
    """Keep only phrases long enough to be a specific watchpoint claim."""
    if isinstance(values, str):
        raw_items = [values]
    elif isinstance(values, (list, tuple)):
        raw_items = list(values)
    else:
        return []
    seen: set[str] = set()
    phrases: list[str] = []
    for item in raw_items:
        phrase = " ".join(str(item or "").split()).strip()
        if len(phrase) < MIN_INVALIDATE_CHARS:
            continue
        key = phrase.lower()
        if key in seen:
            continue
        seen.add(key)
        phrases.append(phrase)
        host = normalize_hostname(phrase)
        if "." in host and host != key and host not in seen and len(host) >= MIN_INVALIDATE_CHARS:
            seen.add(host)
            phrases.append(host)
        www = f"www.{host}"
        if host and "." in host and www not in seen:
            seen.add(www)
            phrases.append(www)
    return phrases


def extract_hostname_corrections(text: str) -> list[dict[str, str]]:
    """Pull explicit 'not host-a but host-b' corrections out of user text.

    Tight on purpose: only hostnames, never free-form entities. That is the
    coprlab.com / copprlab.com incident shape without catching 'it's not ready
    but soon'.
    """
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _NOT_BUT_HOST_RE.finditer(text or ""):
        invalid = normalize_hostname(match.group(1))
        canonical = normalize_hostname(match.group(2))
        if not invalid or not canonical or invalid == canonical:
            continue
        if canonical.endswith(invalid) or invalid.endswith(canonical):
            # Avoid parent/child domain pairs that would self-match.
            continue
        key = (invalid, canonical)
        if key in seen:
            continue
        seen.add(key)
        found.append({"invalidates": invalid, "canonical": canonical})
    return found


def retraction_from_memory_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Read a structured retraction off a memory_write payload.

    Accepts top-level or metadata `invalidates` / `canonical`. Related memory
    without those fields is ignored - that is the future-case guard.
    """
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    invalidates = payload.get("invalidates")
    if invalidates in (None, "", [], ()):
        invalidates = metadata.get("invalidates")
    phrases = sanitize_invalidate_phrases(invalidates)
    if not phrases:
        return None
    canonical = str(payload.get("canonical") or metadata.get("canonical") or "").strip()
    return {
        "invalidates": phrases,
        "canonical": canonical,
        "memory_id": str(
            payload.get("memory_id") or metadata.get("memory_id") or ""
        ).strip(),
        "source": "memory_write",
        "updated_at": utcnow_iso(),
    }


def retractions_from_user_text(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for pair in extract_hostname_corrections(text):
        phrases = sanitize_invalidate_phrases([pair["invalidates"]])
        if not phrases:
            continue
        items.append(
            {
                "invalidates": phrases,
                "canonical": pair["canonical"],
                "memory_id": "",
                "source": "user_text",
                "updated_at": utcnow_iso(),
            }
        )
    return items


def _phrase_hits_line(phrase: str, line: str) -> bool:
    if not phrase or not line:
        return False
    pattern = (
        r"(?<![A-Za-z0-9-])" + re.escape(phrase) + r"(?![A-Za-z0-9-])"
    )
    return re.search(pattern, line, flags=re.IGNORECASE) is not None


def _is_log_section_heading(line: str) -> bool:
    return bool(_LOG_SECTION_RE.match(line.strip()))


def _is_beat_log_line(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _LOG_LINE_MARKERS)


def _supersede_line(line: str, *, invalid: str, canonical: str) -> str:
    stripped = line.lstrip()
    bullet = ""
    rest = stripped
    if stripped.startswith(("- ", "* ")):
        bullet = stripped[:2]
        rest = stripped[2:]
    if rest.upper().startswith(SUPERSEDED_PREFIX):
        return line.rstrip()
    replacement = f"{SUPERSEDED_PREFIX} do not watch {invalid}"
    if canonical:
        replacement += f"; canonical is {canonical}"
    replacement += "."
    leading = line[: len(line) - len(line.lstrip())] if line.strip() else ""
    return f"{leading}{bullet}{replacement}"


def apply_retractions_to_notes(
    notes_text: str,
    retractions: list[dict[str, Any]],
) -> tuple[str, bool, list[dict[str, str]]]:
    """Rewrite standing watchpoints that an explicit retraction invalidates.

    Returns (new_text, changed, applied_summaries). Log sections and beat-log
    lines are not rewritten. A line that already names the canonical value is
    left alone so a corrected watchpoint is not destroyed.
    """
    if not notes_text or not retractions:
        return notes_text or "", False, []

    prepared: list[tuple[list[str], str]] = []
    for item in retractions:
        if not isinstance(item, dict):
            continue
        phrases = sanitize_invalidate_phrases(item.get("invalidates"))
        canonical = str(item.get("canonical") or "").strip()
        if not phrases:
            continue
        prepared.append((phrases, canonical))
    if not prepared:
        return notes_text, False, []

    lines = notes_text.split("\n")
    in_log_section = False
    changed = False
    applied: list[dict[str, str]] = []
    seen_applied: set[tuple[str, str]] = set()

    new_lines: list[str] = []
    for line in lines:
        if _is_log_section_heading(line):
            in_log_section = True
            new_lines.append(line)
            continue
        if in_log_section or _is_beat_log_line(line):
            new_lines.append(line)
            continue

        replaced = line
        for phrases, canonical in prepared:
            canonical_host = normalize_hostname(canonical) if canonical else ""
            if canonical_host and _phrase_hits_line(canonical_host, replaced):
                continue
            hit = next((phrase for phrase in phrases if _phrase_hits_line(phrase, replaced)), None)
            if not hit:
                continue
            replaced = _supersede_line(replaced, invalid=normalize_hostname(hit) or hit, canonical=canonical_host or canonical)
            key = (normalize_hostname(hit) or hit.lower(), canonical_host)
            if key not in seen_applied:
                seen_applied.add(key)
                applied.append(
                    {
                        "invalidates": key[0],
                        "canonical": canonical_host or canonical,
                    }
                )
            break
        if replaced != line:
            changed = True
        new_lines.append(replaced)

    new_text = "\n".join(new_lines)
    if changed and not new_text.endswith("\n") and notes_text.endswith("\n"):
        new_text += "\n"
    return new_text, changed, applied


def upsert_retraction(
    existing: list[dict[str, Any]],
    incoming: dict[str, Any],
) -> list[dict[str, Any]]:
    phrases = sanitize_invalidate_phrases(incoming.get("invalidates"))
    if not phrases:
        return list(existing)
    canonical = str(incoming.get("canonical") or "").strip()
    identity = (
        tuple(sorted(p.lower() for p in phrases)),
        normalize_hostname(canonical),
    )
    merged: list[dict[str, Any]] = []
    replaced = False
    record = {
        "invalidates": phrases,
        "canonical": canonical,
        "memory_id": str(incoming.get("memory_id") or "").strip(),
        "source": str(incoming.get("source") or "unknown").strip() or "unknown",
        "updated_at": str(incoming.get("updated_at") or utcnow_iso()),
    }
    for item in existing:
        if not isinstance(item, dict):
            continue
        other_phrases = sanitize_invalidate_phrases(item.get("invalidates"))
        other_canonical = str(item.get("canonical") or "").strip()
        other_id = (
            tuple(sorted(p.lower() for p in other_phrases)),
            normalize_hostname(other_canonical),
        )
        if other_id == identity:
            merged.append(record)
            replaced = True
        else:
            merged.append(item)
    if not replaced:
        merged.append(record)
    return merged


def load_retractions(path: Path) -> list[dict[str, Any]]:
    with _FILE_LOCK:
        try:
            if not path.exists():
                return []
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    items = payload.get("retractions") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def save_retractions(path: Path, retractions: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"retractions": retractions, "updated_at": utcnow_iso()}
    serialized = json.dumps(payload, indent=2, ensure_ascii=False)
    with _FILE_LOCK:
        path.write_text(serialized + "\n", encoding="utf-8")
