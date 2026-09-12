from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


PROPHET_SCHEMA_VERSION = 1
PROPHET_MIN_STORIES = 5
PROPHET_MAX_STORIES_HARD_CAP = 30
PROPHET_DEFAULT_MAX_STORIES = 15
PROPHET_DEDUP_WINDOW_DAYS = 3
PROPHET_MAX_IMAGES = 4

VALID_LAYOUTS = ("feature", "columns", "briefs", "gallery", "essay")
VALID_ROLES = ("lead", "feature", "standard", "brief", "pull_quote", "image_led")
VALID_SLOTS = ("morning", "evening")
VALID_PAPER_STYLES = ("parchment", "newsprint", "ivory", "midnight")
DEFAULT_PAPER_STYLE = "parchment"

DEFAULT_SECTIONS: tuple[dict[str, Any], ...] = (
    {"id": "breaking", "label": "Breaking Dispatch", "enabled": True},
    {"id": "tech", "label": "Technology & Innovation", "enabled": True},
    {"id": "markets", "label": "Markets & Industry", "enabled": True},
    {"id": "social", "label": "Social Feed", "enabled": True},
    {"id": "science", "label": "Science & Discovery", "enabled": True},
)

_SETTINGS_COLUMN_NAMES = {"sections": "sections_json"}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HEADLINE_KEY_RE = re.compile(r"[^a-z0-9]+")
_PLACEHOLDER_HEADLINE_RE = re.compile(
    r"^(?:test|sample|placeholder|demo|example|untitled|headline|story)\b",
    re.IGNORECASE,
)
_WEAK_IMAGE_HINTS = (
    "favicon",
    "apple-touch-icon",
    "sprite",
    "placeholder",
    "logo",
    "avatar",
    "gravatar",
    "profile_images",
    "profile_image",
    "1x1",
    "pixel.gif",
    "blank.gif",
)


class ProphetValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_edition",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _json_load(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _clean_text(value: Any, *, limit: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_headline_key(headline: str) -> str:
    return _HEADLINE_KEY_RE.sub(" ", headline.casefold()).strip()


def _url_hash(url: str | None) -> str | None:
    normalized = _clean_text(url, limit=2000)
    if not normalized:
        return None
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]


def _normalize_source(raw: Any) -> dict[str, str]:
    if isinstance(raw, str):
        name = _clean_text(raw, limit=200)
        return {"name": name} if name else {}
    if not isinstance(raw, dict):
        return {}
    name = _clean_text(raw.get("name"), limit=200)
    url = _clean_text(raw.get("url"), limit=2000)
    return {key: value for key, value in {"name": name, "url": url}.items() if value}


def is_weak_image_url(url: str | None) -> bool:
    text = _clean_text(url, limit=2000)
    if not text:
        return True
    lowered = text.casefold()
    if lowered.startswith(("data:", "javascript:")) or lowered.endswith(".svg"):
        return True
    return any(hint in lowered for hint in _WEAK_IMAGE_HINTS)


def _normalize_image(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    url = _clean_text(raw.get("url"), limit=2000)
    if not url or is_weak_image_url(url):
        return None
    image: dict[str, str] = {"url": url}
    for key, limit in (("caption", 400), ("credit", 200)):
        value = _clean_text(raw.get(key), limit=limit)
        if value:
            image[key] = value
    return image


def _normalize_body(raw: Any) -> list[str]:
    if isinstance(raw, str):
        candidates: list[Any] = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []
    paragraphs: list[str] = []
    for item in candidates[:4]:
        text = _clean_text(item, limit=1600)
        if text:
            paragraphs.append(text)
    return paragraphs[:3]


def _normalize_story(raw: Any, *, section_id: str | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ProphetValidationError("Each story must be an object.", code="invalid_story")
    headline = _clean_text(raw.get("headline"), limit=300)
    if not headline:
        raise ProphetValidationError("Every story needs a headline.", code="invalid_story")
    role = (_clean_text(raw.get("role")) or "standard").lower()
    if role not in VALID_ROLES:
        role = "standard"
    story: dict[str, Any] = {
        "id": _clean_text(raw.get("id"), limit=80) or f"story_{uuid4().hex[:10]}",
        "headline": headline,
        "role": role,
        "importance": max(0, min(100, _as_int(raw.get("importance"), 50))),
        "body": _normalize_body(raw.get("body")),
        "tags": [
            tag
            for tag in (
                _clean_text(item, limit=60)
                for item in (raw.get("tags") if isinstance(raw.get("tags"), list) else [])
            )
            if tag
        ][:8],
    }
    dek = _clean_text(raw.get("dek"), limit=400)
    if dek:
        story["dek"] = dek
    source = _normalize_source(raw.get("source"))
    if source:
        story["source"] = source
    published_at = _clean_text(raw.get("published_at"), limit=80)
    if published_at:
        story["published_at"] = published_at
    image = _normalize_image(raw.get("image"))
    if image:
        story["image"] = image
    why_selected = _clean_text(raw.get("why_selected"), limit=600)
    if why_selected:
        story["why_selected"] = why_selected
    if section_id:
        story["section_id"] = section_id
    return story


def _story_has_image(story: dict[str, Any]) -> bool:
    image = story.get("image")
    return isinstance(image, dict) and bool(image.get("url"))


def validate_edition_payload(
    payload: Any,
    *,
    max_stories: int = PROPHET_DEFAULT_MAX_STORIES,
    today_iso: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    if not isinstance(payload, dict):
        raise ProphetValidationError("Edition payload must be an object.", code="invalid_edition")

    slot = (_clean_text(payload.get("slot")) or "morning").lower()
    if slot not in VALID_SLOTS:
        raise ProphetValidationError(
            "slot must be one of: morning, evening.",
            code="invalid_slot",
        )

    edition_date = _clean_text(payload.get("edition_date")) or today_iso or datetime.now(
        timezone.utc
    ).date().isoformat()
    if not _DATE_RE.match(edition_date):
        raise ProphetValidationError(
            "edition_date must use YYYY-MM-DD.",
            code="invalid_date",
        )

    sections_raw = payload.get("sections")
    if not isinstance(sections_raw, list) or not sections_raw:
        raise ProphetValidationError(
            "An edition needs at least one section.",
            code="empty_edition",
        )

    sections: list[dict[str, Any]] = []
    all_stories: list[dict[str, Any]] = []
    for raw_section in sections_raw:
        if not isinstance(raw_section, dict):
            continue
        section_id = _clean_text(raw_section.get("id"), limit=60) or f"section_{len(sections) + 1}"
        label = _clean_text(raw_section.get("label"), limit=120) or section_id.replace("_", " ").title()
        layout = (_clean_text(raw_section.get("layout")) or "").lower()
        if layout not in VALID_LAYOUTS:
            layout = "columns"
        stories_raw = raw_section.get("stories")
        stories = [
            _normalize_story(item, section_id=section_id)
            for item in (stories_raw if isinstance(stories_raw, list) else [])
        ]
        if not stories:
            continue
        with_images = sum(1 for story in stories if _story_has_image(story))
        if layout == "gallery" and with_images < 2:
            layout = "columns"
            warnings.append(f"Section '{section_id}' gallery needs at least two images; rendered as columns.")
        section: dict[str, Any] = {
            "id": section_id,
            "label": label,
            "layout": layout,
            "stories": stories,
        }
        quiet_day_text = _clean_text(raw_section.get("quiet_day_text"), limit=240)
        if quiet_day_text:
            section["quiet_day_text"] = quiet_day_text
        sections.append(section)
        all_stories.extend(stories)

    for story in all_stories:
        role = story.get("role")
        if role == "brief" and story.get("body"):
            story["body"] = []
            warnings.append(f"Brief '{story['headline'][:60]}' had body text; trimmed to a one-line brief.")
        if role == "image_led" and not _story_has_image(story):
            story["role"] = "standard"
            warnings.append(f"Image-led '{story['headline'][:60]}' had no image; downgraded to standard.")

    lead_raw = payload.get("lead")
    lead: dict[str, Any] | None = None
    if isinstance(lead_raw, dict):
        lead = _normalize_story(lead_raw, section_id=None)
        lead["role"] = "lead"
    else:
        lead_candidates = sorted(
            (story for story in all_stories if story.get("role") == "lead"),
            key=lambda item: item.get("importance", 0),
            reverse=True,
        )
        if lead_candidates:
            lead = lead_candidates[0]
            for section in sections:
                section["stories"] = [
                    story for story in section["stories"] if story["id"] != lead["id"]
                ]
            warning_stories = [story for story in all_stories if story["id"] != lead["id"]]
            for story in warning_stories:
                if story.get("role") == "lead":
                    story["role"] = "feature"
            sections = [section for section in sections if section["stories"]]
            warnings.append("Promoted the highest-importance lead-role story to the front page.")
        else:
            ranked = sorted(all_stories, key=lambda item: item.get("importance", 0), reverse=True)
            if ranked:
                lead = ranked[0]
                for section in sections:
                    section["stories"] = [
                        story for story in section["stories"] if story["id"] != lead["id"]
                    ]
                sections = [section for section in sections if section["stories"]]
                warnings.append("No lead was provided; promoted the highest-importance story to the front page.")

    total_stories = (1 if lead else 0) + sum(len(section["stories"]) for section in sections)
    if total_stories <= 0:
        raise ProphetValidationError("An edition needs stories.", code="empty_edition")
    if total_stories > max_stories:
        overflow = total_stories - max_stories
        removable = sorted(
            (
                (section, story)
                for section in sections
                for story in section["stories"]
            ),
            key=lambda item: item[1].get("importance", 0),
        )
        removed = 0
        for section, story in removable:
            if removed >= overflow:
                break
            section["stories"] = [
                item for item in section["stories"] if item is not story
            ]
            removed += 1
        sections = [section for section in sections if section["stories"]]
        if removed:
            warnings.append(
                f"Trimmed {removed} lower-importance stories to honor the {max_stories}-story cap."
            )
        total_stories = (1 if lead else 0) + sum(
            len(section["stories"]) for section in sections
        )
        if total_stories <= 0:
            raise ProphetValidationError("An edition needs stories.", code="empty_edition")

    published_headlines = [
        story["headline"]
        for story in (
            ([lead] if lead else [])
            + [story for section in sections for story in section["stories"]]
        )
    ]
    if published_headlines:
        placeholder_hits = [
            headline
            for headline in published_headlines
            if _PLACEHOLDER_HEADLINE_RE.match(headline)
        ]
        if len(placeholder_hits) == len(published_headlines):
            raise ProphetValidationError(
                "This edition looks like placeholder or test content. Publish the real edition.",
                code="placeholder_edition",
            )

    def _image_stories() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if lead is not None and _story_has_image(lead):
            items.append(lead)
        for section in sections:
            for story in section["stories"]:
                if _story_has_image(story):
                    items.append(story)
        return items

    seen_urls: set[str] = set()
    for story in _image_stories():
        image_url = str((story.get("image") or {}).get("url") or "")
        if image_url and image_url in seen_urls:
            story.pop("image", None)
            warnings.append(f"Removed a duplicate image from '{story['headline'][:60]}'.")
            continue
        seen_urls.add(image_url)

    visual_stories = _image_stories()
    if len(visual_stories) > PROPHET_MAX_IMAGES:
        keep_ids: set[int] = set()
        if lead is not None and _story_has_image(lead):
            keep_ids.add(id(lead))
        ranked = sorted(
            (story for story in visual_stories if id(story) not in keep_ids),
            key=lambda item: item.get("importance", 0),
            reverse=True,
        )
        for story in ranked[: max(0, PROPHET_MAX_IMAGES - len(keep_ids))]:
            keep_ids.add(id(story))
        for story in visual_stories:
            if id(story) not in keep_ids:
                story.pop("image", None)
                warnings.append(
                    f"Trimmed an extra image from '{story['headline'][:60]}' to keep the paper clean."
                )

    normalized: dict[str, Any] = {
        "schema_version": PROPHET_SCHEMA_VERSION,
        "edition_date": edition_date,
        "slot": slot,
        "sections": sections,
    }
    if lead:
        normalized["lead"] = lead
    editor_note = _clean_text(payload.get("editor_note"), limit=2000)
    if editor_note:
        normalized["editor_note"] = editor_note
    footer = _clean_text(payload.get("footer"), limit=300)
    if footer:
        normalized["footer"] = footer
    return normalized, warnings


def normalize_settings_changes(changes: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if "enabled" in changes:
        clean["enabled"] = _as_bool(changes.get("enabled"), True)
    if "evening_enabled" in changes:
        clean["evening_enabled"] = _as_bool(changes.get("evening_enabled"), True)
    if "notifications_enabled" in changes:
        clean["notifications_enabled"] = _as_bool(changes.get("notifications_enabled"), True)
    for key in ("morning_time", "evening_time"):
        if key in changes:
            value = _clean_text(changes.get(key))
            if not value or not _TIME_RE.match(value):
                raise ProphetValidationError(
                    f"{key} must use HH:MM in 24-hour local time.",
                    code="invalid_time",
                )
            clean[key] = value
    if "max_stories" in changes:
        try:
            count = int(changes.get("max_stories"))
        except (TypeError, ValueError):
            raise ProphetValidationError("max_stories must be a number.", code="invalid_max_stories")
        if count < PROPHET_MIN_STORIES or count > PROPHET_MAX_STORIES_HARD_CAP:
            raise ProphetValidationError(
                f"max_stories must be between {PROPHET_MIN_STORIES} and {PROPHET_MAX_STORIES_HARD_CAP}.",
                code="invalid_max_stories",
            )
        clean["max_stories"] = count
    if "paper_style" in changes:
        paper_style = (_clean_text(changes.get("paper_style")) or "").lower()
        if paper_style not in VALID_PAPER_STYLES:
            raise ProphetValidationError(
                "paper_style must be one of: " + ", ".join(VALID_PAPER_STYLES) + ".",
                code="invalid_paper_style",
            )
        clean["paper_style"] = paper_style
    if "sections" in changes and isinstance(changes.get("sections"), list):
        sections: list[dict[str, Any]] = []
        for raw in changes["sections"]:
            if not isinstance(raw, dict):
                continue
            section_id = _clean_text(raw.get("id"), limit=60)
            label = _clean_text(raw.get("label"), limit=120)
            if not section_id:
                continue
            sections.append(
                {
                    "id": section_id,
                    "label": label or section_id.replace("_", " ").title(),
                    "enabled": _as_bool(raw.get("enabled"), True),
                }
            )
        if sections:
            clean["sections"] = sections
    return clean


class ProphetStore:
    """SQLite-backed store for Daily Prophet settings, preferences, and editions."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS prophet_settings (
                    config_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    morning_time TEXT NOT NULL DEFAULT '05:00',
                    evening_enabled INTEGER NOT NULL DEFAULT 1,
                    evening_time TEXT NOT NULL DEFAULT '19:00',
                    max_stories INTEGER NOT NULL DEFAULT 15,
                    notifications_enabled INTEGER NOT NULL DEFAULT 1,
                    paper_style TEXT NOT NULL DEFAULT 'parchment',
                    sections_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prophet_interests (
                    topic TEXT PRIMARY KEY,
                    origin TEXT NOT NULL DEFAULT 'inferred',
                    weight REAL NOT NULL DEFAULT 0.5,
                    muted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prophet_sources (
                    source_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL DEFAULT 'site',
                    value TEXT NOT NULL,
                    label TEXT,
                    origin TEXT NOT NULL DEFAULT 'inferred',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kind, value)
                );

                CREATE TABLE IF NOT EXISTS prophet_editions (
                    edition_id TEXT PRIMARY KEY,
                    edition_date TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'published',
                    payload_json TEXT NOT NULL,
                    editor_note TEXT,
                    story_count INTEGER NOT NULL DEFAULT 0,
                    generated_by_request_id TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(edition_date, slot)
                );

                CREATE INDEX IF NOT EXISTS idx_prophet_editions_date
                    ON prophet_editions(edition_date DESC, slot ASC);
                CREATE INDEX IF NOT EXISTS idx_prophet_editions_request
                    ON prophet_editions(generated_by_request_id);

                CREATE TABLE IF NOT EXISTS prophet_story_index (
                    story_row_id TEXT PRIMARY KEY,
                    edition_id TEXT NOT NULL,
                    edition_date TEXT NOT NULL,
                    slot TEXT NOT NULL,
                    story_id TEXT,
                    url_hash TEXT,
                    headline TEXT NOT NULL,
                    headline_key TEXT NOT NULL,
                    section_id TEXT,
                    shown_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_prophet_story_index_shown
                    ON prophet_story_index(shown_at DESC);
                CREATE INDEX IF NOT EXISTS idx_prophet_story_index_url
                    ON prophet_story_index(url_hash);
                CREATE INDEX IF NOT EXISTS idx_prophet_story_index_headline
                    ON prophet_story_index(headline_key);
                """
            )
            now = utcnow_iso()
            self._ensure_settings_columns(connection)
            connection.execute(
                """
                INSERT INTO prophet_settings (
                    config_id,
                    enabled,
                    morning_time,
                    evening_enabled,
                    evening_time,
                    max_stories,
                    notifications_enabled,
                    paper_style,
                    sections_json,
                    updated_at
                )
                VALUES ('default', 1, '05:00', 1, '19:00', ?, 1, ?, ?, ?)
                ON CONFLICT(config_id) DO NOTHING
                """,
                (
                    PROPHET_DEFAULT_MAX_STORIES,
                    DEFAULT_PAPER_STYLE,
                    _json_dumps(list(DEFAULT_SECTIONS)),
                    now,
                ),
            )
            connection.commit()

    def get_settings(self) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM prophet_settings WHERE config_id = 'default'"
            ).fetchone()
        if row is None:
            raise RuntimeError("prophet settings have not been initialized")
        record = dict(row)
        sections = _json_load(record.pop("sections_json", None), list(DEFAULT_SECTIONS))
        record["sections"] = sections if isinstance(sections, list) and sections else list(DEFAULT_SECTIONS)
        record["enabled"] = bool(record.get("enabled"))
        record["evening_enabled"] = bool(record.get("evening_enabled"))
        record["notifications_enabled"] = bool(record.get("notifications_enabled"))
        record["max_stories"] = max(
            PROPHET_MIN_STORIES,
            min(PROPHET_MAX_STORIES_HARD_CAP, _as_int(record.get("max_stories"), PROPHET_DEFAULT_MAX_STORIES)),
        )
        paper_style = (_clean_text(record.get("paper_style")) or "").lower()
        record["paper_style"] = (
            paper_style if paper_style in VALID_PAPER_STYLES else DEFAULT_PAPER_STYLE
        )
        return record

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        clean = normalize_settings_changes(changes)
        if not clean:
            return self.get_settings()
        now = utcnow_iso()
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in clean.items():
            column = _SETTINGS_COLUMN_NAMES.get(key, key)
            assignments.append(f"{column} = ?")
            values.append(_json_dumps(value) if column == "sections_json" else value)
        assignments.append("updated_at = ?")
        values.append(now)
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                f"UPDATE prophet_settings SET {', '.join(assignments)} WHERE config_id = 'default'",
                tuple(values),
            )
            connection.commit()
        return self.get_settings()

    def list_interests(self, *, include_muted: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM prophet_interests"
        if not include_muted:
            query += " WHERE muted = 0"
        query += " ORDER BY weight DESC, topic ASC"
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query).fetchall()
        return [
            {
                **dict(row),
                "muted": bool(row["muted"]),
                "weight": float(row["weight"] or 0.5),
            }
            for row in rows
        ]

    def upsert_interest(
        self,
        topic: str,
        *,
        origin: str = "inferred",
        weight: float | None = None,
        muted: bool | None = None,
    ) -> dict[str, Any] | None:
        normalized = _clean_text(topic, limit=120)
        if not normalized:
            return None
        normalized_origin = origin if origin in {"user", "inferred"} else "inferred"
        now = utcnow_iso()
        with self._lock, closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM prophet_interests WHERE topic = ?",
                (normalized,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO prophet_interests (topic, origin, weight, muted, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized,
                        normalized_origin,
                        max(0.0, min(1.0, float(weight if weight is not None else 0.5))),
                        1 if muted else 0,
                        now,
                        now,
                    ),
                )
            else:
                next_origin = "user" if existing["origin"] == "user" or normalized_origin == "user" else "inferred"
                next_weight = (
                    max(0.0, min(1.0, float(weight)))
                    if weight is not None
                    else float(existing["weight"] or 0.5)
                )
                next_muted = int(bool(muted)) if muted is not None else int(existing["muted"] or 0)
                connection.execute(
                    """
                    UPDATE prophet_interests
                    SET origin = ?, weight = ?, muted = ?, updated_at = ?
                    WHERE topic = ?
                    """,
                    (next_origin, next_weight, next_muted, now, normalized),
                )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM prophet_interests WHERE topic = ?",
                (normalized,),
            ).fetchone()
        return dict(row) if row else None

    def remove_interest(self, topic: str) -> bool:
        normalized = _clean_text(topic, limit=120)
        if not normalized:
            return False
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM prophet_interests WHERE topic = ? AND origin != 'user'",
                (normalized,),
            )
            connection.commit()
        return bool(cursor.rowcount)

    def delete_interest(self, topic: str) -> bool:
        normalized = _clean_text(topic, limit=120)
        if not normalized:
            return False
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM prophet_interests WHERE topic = ?",
                (normalized,),
            )
            connection.commit()
        return bool(cursor.rowcount)

    def list_sources(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM prophet_sources"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY kind ASC, value ASC"
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query).fetchall()
        return [{**dict(row), "active": bool(row["active"])} for row in rows]

    def upsert_source(
        self,
        *,
        kind: str,
        value: str,
        label: str | None = None,
        origin: str = "inferred",
    ) -> dict[str, Any] | None:
        normalized_kind = (_clean_text(kind) or "site").lower()
        if normalized_kind not in {"rss", "x", "site"}:
            normalized_kind = "site"
        normalized_value = _clean_text(value, limit=500)
        if not normalized_value:
            return None
        normalized_origin = origin if origin in {"user", "inferred"} else "inferred"
        normalized_label = _clean_text(label, limit=160)
        now = utcnow_iso()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO prophet_sources (source_id, kind, value, label, origin, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(kind, value) DO UPDATE SET
                    label = COALESCE(excluded.label, prophet_sources.label),
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    f"psrc_{uuid4().hex[:12]}",
                    normalized_kind,
                    normalized_value,
                    normalized_label,
                    normalized_origin,
                    now,
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM prophet_sources WHERE kind = ? AND value = ?",
                (normalized_kind, normalized_value),
            ).fetchone()
        return dict(row) if row else None

    def remove_source(self, source_id: str) -> bool:
        normalized = _clean_text(source_id, limit=80)
        if not normalized:
            return False
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM prophet_sources WHERE source_id = ?",
                (normalized,),
            )
            connection.commit()
        return bool(cursor.rowcount)

    def remove_source_by_value(self, *, kind: str, value: str) -> bool:
        normalized_kind = (_clean_text(kind) or "").lower()
        normalized_value = _clean_text(value, limit=500)
        if not normalized_kind or not normalized_value:
            return False
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM prophet_sources WHERE kind = ? AND value = ?",
                (normalized_kind, normalized_value),
            )
            connection.commit()
        return bool(cursor.rowcount)

    def publish_edition(
        self,
        payload: Any,
        *,
        request_id: str | None = None,
        slot: str | None = None,
    ) -> dict[str, Any]:
        settings = self.get_settings()
        max_stories = int(settings.get("max_stories") or PROPHET_DEFAULT_MAX_STORIES)
        candidate = dict(payload) if isinstance(payload, dict) else {}
        if slot and not candidate.get("slot"):
            candidate["slot"] = slot
        normalized, warnings = validate_edition_payload(candidate, max_stories=max_stories)
        edition_date = normalized["edition_date"]
        edition_slot = normalized["slot"]
        warnings.extend(
            self._dedup_warnings(
                normalized,
                edition_date=edition_date,
                slot=edition_slot,
            )
        )

        now = utcnow_iso()
        story_count = (1 if normalized.get("lead") else 0) + sum(
            len(section.get("stories") or []) for section in normalized.get("sections") or []
        )
        editor_note = normalized.get("editor_note")
        payload_json = _json_dumps(normalized)
        with self._lock, closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT * FROM prophet_editions WHERE edition_date = ? AND slot = ?",
                (edition_date, edition_slot),
            ).fetchone()
            if existing is not None:
                edition_id = str(existing["edition_id"])
                revision = int(existing["revision"] or 1) + 1
                connection.execute(
                    """
                    UPDATE prophet_editions
                    SET status = 'published',
                        payload_json = ?,
                        editor_note = ?,
                        story_count = ?,
                        generated_by_request_id = ?,
                        revision = ?,
                        updated_at = ?
                    WHERE edition_id = ?
                    """,
                    (
                        payload_json,
                        editor_note,
                        story_count,
                        _clean_text(request_id, limit=120),
                        revision,
                        now,
                        edition_id,
                    ),
                )
            else:
                edition_id = f"ped_{uuid4().hex[:12]}"
                revision = 1
                connection.execute(
                    """
                    INSERT INTO prophet_editions (
                        edition_id,
                        edition_date,
                        slot,
                        schema_version,
                        status,
                        payload_json,
                        editor_note,
                        story_count,
                        generated_by_request_id,
                        revision,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, 'published', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        edition_id,
                        edition_date,
                        edition_slot,
                        PROPHET_SCHEMA_VERSION,
                        payload_json,
                        editor_note,
                        story_count,
                        _clean_text(request_id, limit=120),
                        revision,
                        now,
                        now,
                    ),
                )
            connection.execute(
                "DELETE FROM prophet_story_index WHERE edition_id = ?",
                (edition_id,),
            )
            for story in self._edition_stories(normalized):
                headline = str(story.get("headline") or "").strip()
                if not headline:
                    continue
                source = story.get("source") if isinstance(story.get("source"), dict) else {}
                url = _clean_text(source.get("url"), limit=2000) if source else None
                connection.execute(
                    """
                    INSERT INTO prophet_story_index (
                        story_row_id,
                        edition_id,
                        edition_date,
                        slot,
                        story_id,
                        url_hash,
                        headline,
                        headline_key,
                        section_id,
                        shown_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"pstory_{uuid4().hex[:12]}",
                        edition_id,
                        edition_date,
                        edition_slot,
                        _clean_text(story.get("id"), limit=80),
                        _url_hash(url),
                        headline,
                        _normalize_headline_key(headline),
                        _clean_text(story.get("section_id"), limit=60),
                        now,
                    ),
                )
            connection.commit()
        record = self.get_edition(edition_date, edition_slot)
        return {
            "edition_id": edition_id,
            "edition": normalized,
            "edition_date": edition_date,
            "slot": edition_slot,
            "story_count": story_count,
            "revision": revision,
            "warnings": warnings,
            "published_at": now,
            "created": existing is None,
            "edition_record": record,
        }

    def get_edition(self, edition_date: str | None = None, slot: str | None = None) -> dict[str, Any] | None:
        with self._lock, closing(self._connect()) as connection:
            if edition_date:
                normalized_slot = (_clean_text(slot) or "").lower()
                if normalized_slot in VALID_SLOTS:
                    row = connection.execute(
                        "SELECT * FROM prophet_editions WHERE edition_date = ? AND slot = ?",
                        (edition_date, normalized_slot),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT * FROM prophet_editions
                        WHERE edition_date = ?
                        ORDER BY CASE slot WHEN 'evening' THEN 0 ELSE 1 END, updated_at DESC
                        LIMIT 1
                        """,
                        (edition_date,),
                    ).fetchone()
            elif _clean_text(slot) and (_clean_text(slot) or "").lower() in VALID_SLOTS:
                row = connection.execute(
                    """
                    SELECT * FROM prophet_editions
                    WHERE slot = ?
                    ORDER BY edition_date DESC, updated_at DESC
                    LIMIT 1
                    """,
                    ((_clean_text(slot) or "").lower(),),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM prophet_editions
                    ORDER BY edition_date DESC, updated_at DESC
                    LIMIT 1
                    """
                ).fetchone()
        return self._row_to_edition(row)

    def find_edition_by_request_id(self, request_id: str | None) -> dict[str, Any] | None:
        normalized = _clean_text(request_id, limit=120)
        if not normalized:
            return None
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM prophet_editions
                WHERE generated_by_request_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        return self._row_to_edition(row)

    def list_editions(self, *, limit: int = 10, days: int | None = None) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(60, int(limit or 10)))
        query = "SELECT * FROM prophet_editions"
        params: list[Any] = []
        if days is not None and int(days) > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).date().isoformat()
            query += " WHERE edition_date >= ?"
            params.append(cutoff)
        query += " ORDER BY edition_date DESC, updated_at DESC LIMIT ?"
        params.append(bounded_limit)
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            edition = self._row_to_edition(row)
            if edition is None:
                continue
            stories = self._edition_stories(edition.get("payload") or {})
            summaries.append(
                {
                    "edition_id": edition["edition_id"],
                    "edition_date": edition["edition_date"],
                    "slot": edition["slot"],
                    "story_count": edition["story_count"],
                    "editor_note": edition.get("editor_note"),
                    "revision": edition["revision"],
                    "published_at": edition["updated_at"],
                    "headlines": [
                        {
                            "headline": story.get("headline"),
                            "section_id": story.get("section_id"),
                            "url": (story.get("source") or {}).get("url")
                            if isinstance(story.get("source"), dict)
                            else None,
                            "role": story.get("role"),
                        }
                        for story in stories[:24]
                    ],
                }
            )
        return summaries

    def recent_story_summaries(self, *, days: int = PROPHET_DEDUP_WINDOW_DAYS, limit: int = 120) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat().replace(
            "+00:00", "Z"
        )
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT edition_date, slot, headline, headline_key, section_id, url_hash, shown_at
                FROM prophet_story_index
                WHERE shown_at >= ?
                ORDER BY shown_at DESC
                LIMIT ?
                """,
                (cutoff, max(1, min(400, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        settings = self.get_settings()
        with self._lock, closing(self._connect()) as connection:
            edition_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM prophet_editions").fetchone()["count"]
            )
            interest_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM prophet_interests").fetchone()["count"]
            )
            source_count = int(
                connection.execute("SELECT COUNT(*) AS count FROM prophet_sources").fetchone()["count"]
            )
        return {
            "db_path": str(self.db_path),
            "enabled": settings["enabled"],
            "morning_time": settings["morning_time"],
            "evening_enabled": settings["evening_enabled"],
            "evening_time": settings["evening_time"],
            "max_stories": settings["max_stories"],
            "paper_style": settings["paper_style"],
            "edition_count": edition_count,
            "interest_count": interest_count,
            "source_count": source_count,
        }

    def _dedup_warnings(
        self,
        edition: dict[str, Any],
        *,
        edition_date: str,
        slot: str,
    ) -> list[str]:
        recent = self.recent_story_summaries(days=PROPHET_DEDUP_WINDOW_DAYS, limit=200)
        recent = [
            row
            for row in recent
            if not (row.get("edition_date") == edition_date and row.get("slot") == slot)
        ]
        if not recent:
            return []
        url_hashes = {row["url_hash"] for row in recent if row.get("url_hash")}
        headline_keys = {str(row["headline_key"]) for row in recent if row.get("headline_key")}
        warnings: list[str] = []
        for story in self._edition_stories(edition):
            headline = str(story.get("headline") or "").strip()
            if not headline:
                continue
            source = story.get("source") if isinstance(story.get("source"), dict) else {}
            url = _clean_text(source.get("url"), limit=2000) if source else None
            digest = _url_hash(url)
            key = _normalize_headline_key(headline)
            if (digest and digest in url_hashes) or key in headline_keys:
                warnings.append(f"'{headline[:70]}' appeared in a recent edition.")
        return warnings

    @staticmethod
    def _edition_stories(edition: dict[str, Any]) -> list[dict[str, Any]]:
        stories: list[dict[str, Any]] = []
        lead = edition.get("lead")
        if isinstance(lead, dict):
            stories.append({**lead, "section_id": None})
        for section in edition.get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_id = section.get("id")
            for story in section.get("stories") or []:
                if isinstance(story, dict):
                    stories.append({**story, "section_id": section_id})
        return stories

    @staticmethod
    def _row_to_edition(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        record["payload"] = _json_load(record.pop("payload_json", None), {})
        return record

    @staticmethod
    def _ensure_settings_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(prophet_settings)")
        }
        if "paper_style" not in columns:
            connection.execute(
                "ALTER TABLE prophet_settings ADD COLUMN paper_style TEXT NOT NULL DEFAULT 'parchment'"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection
