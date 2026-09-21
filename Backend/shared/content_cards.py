from __future__ import annotations

from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

CONTENT_CARD_TYPE = "content_card"
CONTENT_CARD_SCHEMA_VERSION = 1

MAX_CARDS_PER_BATCH = 5
MAX_SECTIONS_PER_CARD = 8
MAX_TITLE_CHARS = 120
MAX_SUBTITLE_CHARS = 200
MAX_TEXT_CHARS = 4000
MAX_CODE_CHARS = 6000
MAX_LIST_ITEMS = 12
# List/checklist rows carry instructions ("Demo: 60-90s clip of ..."), not
# chips; an 80-char clip amputated them mid-sentence. Chips keep the 80 cap.
MAX_LIST_ITEM_CHARS = 240
MAX_CHIPS = 16
MAX_KEY_VALUES = 12
MAX_ACTIONS = 3
MAX_FALLBACK_CHARS = 8000
MAX_BATCH_SERIALIZED_CHARS = 24_000

KNOWN_PRESETS = frozenset({"social_post", "copy_payload", "option_set", "checklist"})
KNOWN_BRANDS = frozenset({"x", "gmail", "github", "generic"})
ALLOWED_SECTION_TYPES = frozenset({"text", "key_value", "chips", "list", "code", "quote"})
ALLOWED_ACTIONS = frozenset({"copy", "open_url"})
PRIVILEGED_ACTIONS = frozenset({
    "send",
    "post",
    "schedule",
    "approve",
    "reject",
    "deny",
    "grant",
    "rsvp",
    "delete",
    "share",
    "publish",
})
SOCIAL_CHARACTER_LIMITS = {
    "x": 280,
    "linkedin": 3000,
}

SPECIALIST_VIEW_REGISTRY = frozenset({"cosmic/x-search-results:v1"})
INTENT_PRESENTATION = {
    "x.search": {
        "view": "cosmic/x-search-results:v1",
        "data_path": "notable_posts",
        "covers": ["notable posts", "excerpts", "authors", "post links"],
        "response_mode": "synthesis_without_item_duplication",
        "instruction": (
            "The client will render notable X posts as native cards beside your final response. "
            "Write a short synthesis. Do not paste the post excerpts, handles, or links already shown in the cards."
        ),
    }
}


def channel_supports_trusted_ui(channel: str | None) -> bool:
    platform = str(channel or "").strip().lower().split(":", 1)[0]
    return platform in {"desktop", "mobile"}


def normalize_content_cards(raw_cards: Any, *, id_prefix: str = "content_card") -> dict[str, Any]:
    if not isinstance(raw_cards, list) or not raw_cards:
        return {"error": True, "message": "cards must be a non-empty array."}
    if len(raw_cards) > MAX_CARDS_PER_BATCH:
        return {
            "error": True,
            "message": f"Present at most {MAX_CARDS_PER_BATCH} cards in one call.",
        }

    blocks: list[dict[str, Any]] = []
    dropped = 0
    for index, raw in enumerate(raw_cards, start=1):
        block = _normalize_content_card(raw, assigned_id=f"{id_prefix}_{uuid4().hex[:12]}")
        if block is None:
            dropped += 1
            continue
        blocks.append(block)

    if not blocks:
        return {"error": True, "message": "No valid content cards could be built from the payload."}

    serialized = _rough_size(blocks)
    if serialized > MAX_BATCH_SERIALIZED_CHARS:
        return {"error": True, "message": "Content card payload is too large."}

    covers: list[str] = []
    for block in blocks:
        title = str(block.get("title") or "").strip()
        if title and title not in covers:
            covers.append(title)
        preset = str(block.get("preset") or "").strip()
        if preset == "social_post" and "post body" not in covers:
            covers.append("post body")
            covers.append("hashtags")
            covers.append("mentions")
        elif "card body" not in covers:
            covers.append("card body")

    return {
        "status": "presented",
        "card_count": len(blocks),
        "dropped_count": dropped,
        "response_blocks": blocks,
        "covers": covers[:12],
    }


def project_specialist_content_cards(
    *,
    intent: str,
    response: dict[str, Any],
    presentation: dict[str, Any] | None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    if not channel_supports_trusted_ui(channel):
        return None
    presentation = presentation if isinstance(presentation, dict) else INTENT_PRESENTATION.get(intent)
    if not isinstance(presentation, dict):
        return None
    view = str(presentation.get("view") or "").strip()
    data_path = str(presentation.get("data_path") or "").strip()
    if view not in SPECIALIST_VIEW_REGISTRY or not data_path:
        return None
    if view == "cosmic/x-search-results:v1":
        raw_cards = _x_search_result_cards(response, data_path=data_path)
    else:
        return None
    if not raw_cards:
        return None
    normalized = normalize_content_cards(raw_cards, id_prefix="x_post")
    if normalized.get("error"):
        return None
    return normalized


def merge_content_cards_into_response_blocks(
    blocks: list[dict[str, Any]] | None,
    cards: list[dict[str, Any]] | None,
    *,
    display_text: str | None = None,
) -> list[dict[str, Any]] | None:
    valid_cards = [
        item
        for item in (cards or [])
        if isinstance(item, dict) and str(item.get("type") or "") == CONTENT_CARD_TYPE
    ]
    if not valid_cards:
        return blocks

    from .response_blocks import build_response_blocks

    base: list[dict[str, Any]] = [item for item in (blocks or []) if isinstance(item, dict)]
    if not base and str(display_text or "").strip():
        base = build_response_blocks(display_text)

    existing_ids = {str(item.get("id") or "").strip() for item in base if str(item.get("id") or "").strip()}
    for card in valid_cards:
        card_id = str(card.get("id") or "").strip()
        if card_id and card_id in existing_ids:
            continue
        base.append(card)
        if card_id:
            existing_ids.add(card_id)
    return base or None


def extract_content_card_blocks(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    blocks = payload.get("response_blocks")
    if not isinstance(blocks, list):
        return []
    return [
        item
        for item in blocks
        if isinstance(item, dict) and str(item.get("type") or "") == CONTENT_CARD_TYPE
    ]


def content_card_presentation_contract(
    *,
    covers: list[str],
    card_count: int,
) -> dict[str, Any]:
    noun = "card" if card_count == 1 else "cards"
    return {
        "version": 1,
        "render": "trusted_inline_block",
        "block_type": CONTENT_CARD_TYPE,
        "covers": covers or ["card body"],
        "response_mode": "brief_acknowledgement",
        "instruction": (
            f"The client will render {card_count} native content {noun} beside your final response. "
            "Briefly introduce why they matter. Do not repeat the card titles, bodies, hashtags, "
            "or other covered fields in Markdown."
        ),
    }


def _normalize_content_card(raw: Any, *, assigned_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = _clip(_safe_text(raw.get("title")), MAX_TITLE_CHARS)
    subtitle = _clip(_safe_text(raw.get("subtitle")), MAX_SUBTITLE_CHARS)
    preset = _normalize_preset(raw.get("preset"))
    brand = _normalize_brand(raw.get("brand") or raw.get("platform"))
    body = _clip(_safe_text(raw.get("body")), MAX_TEXT_CHARS)
    tags = _string_list(raw.get("tags"), limit=MAX_CHIPS)
    mentions = _string_list(raw.get("mentions"), limit=MAX_CHIPS)
    sections = _normalize_sections(raw.get("sections"))
    if not sections:
        sections = _sections_from_preset(preset, body=body, tags=tags, mentions=mentions, raw=raw)
    if not title and not sections and not body:
        return None
    if not title:
        title = _default_title(preset, brand)

    actions = _normalize_actions(raw.get("actions"), default_copy_text=body or _first_text(sections))
    group = _normalize_group(raw)
    character_limit = _coerce_limit(raw.get("limit") or raw.get("character_limit"), brand=brand, preset=preset)
    character_count = _character_count(body) if body else _character_count(_first_text(sections))
    fallback_text = _clip(_build_fallback_text(
        title=title,
        subtitle=subtitle,
        body=body,
        sections=sections,
        tags=tags,
        mentions=mentions,
    ), MAX_FALLBACK_CHARS)

    block: dict[str, Any] = {
        "id": assigned_id,
        "type": CONTENT_CARD_TYPE,
        "schema_version": CONTENT_CARD_SCHEMA_VERSION,
        "title": title,
        "sections": sections,
        "actions": actions,
        "fallback_text": fallback_text,
    }
    if preset:
        block["preset"] = preset
    if brand:
        block["brand"] = brand
    if subtitle:
        block["subtitle"] = subtitle
    if body:
        block["body"] = body
    if tags:
        block["tags"] = tags
    if mentions:
        block["mentions"] = mentions
    if character_limit:
        block["character_limit"] = character_limit
        block["character_count"] = character_count
    if group:
        block["group"] = group
    return block


def _sections_from_preset(
    preset: str | None,
    *,
    body: str,
    tags: list[str],
    mentions: list[str],
    raw: dict[str, Any],
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    if body:
        sections.append({"type": "text", "text": body})
    chips = [*tags, *mentions]
    if chips:
        sections.append({"type": "chips", "items": chips[:MAX_CHIPS]})
    if preset == "checklist":
        items = _string_list(raw.get("items") or raw.get("options"), limit=MAX_LIST_ITEMS, max_item_chars=MAX_LIST_ITEM_CHARS)
        if items:
            sections.append({"type": "list", "style": "checklist", "items": items})
    elif preset == "option_set":
        items = _string_list(raw.get("options") or raw.get("items"), limit=MAX_LIST_ITEMS, max_item_chars=MAX_LIST_ITEM_CHARS)
        if items:
            sections.append({"type": "list", "style": "options", "items": items})
    return sections[:MAX_SECTIONS_PER_CARD]


def _normalize_sections(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    sections: list[dict[str, Any]] = []
    for item in raw[:MAX_SECTIONS_PER_CARD]:
        section = _normalize_section(item)
        if section:
            sections.append(section)
    return sections


def _normalize_section(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    section_type = str(raw.get("type") or "").strip().lower().replace("-", "_")
    if section_type not in ALLOWED_SECTION_TYPES:
        return None
    if section_type in {"text", "quote"}:
        text = _clip(_safe_text(raw.get("text") or raw.get("body")), MAX_TEXT_CHARS)
        if not text:
            return None
        return {"type": section_type, "text": text}
    if section_type == "code":
        code = _clip(_safe_text(raw.get("code") or raw.get("text")), MAX_CODE_CHARS)
        if not code:
            return None
        language = _clip(_safe_text(raw.get("language")), 32) or None
        section = {"type": "code", "code": code}
        if language:
            section["language"] = language
        return section
    if section_type == "chips":
        items = _string_list(raw.get("items") or raw.get("chips"), limit=MAX_CHIPS)
        if not items:
            return None
        return {"type": "chips", "items": items}
    if section_type == "list":
        items = _string_list(raw.get("items"), limit=MAX_LIST_ITEMS, max_item_chars=MAX_LIST_ITEM_CHARS)
        if not items:
            return None
        style = str(raw.get("style") or "").strip().lower()
        section: dict[str, Any] = {"type": "list", "items": items}
        if style in {"checklist", "options", "bullets"}:
            section["style"] = style
        return section
    if section_type == "key_value":
        rows = _normalize_key_values(raw.get("rows") or raw.get("items") or raw.get("fields"))
        if not rows:
            return None
        return {"type": "key_value", "rows": rows}
    return None


def _normalize_key_values(raw: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if isinstance(raw, dict):
        for key, value in list(raw.items())[:MAX_KEY_VALUES]:
            label = _clip(_safe_text(key), 40)
            text = _clip(_safe_text(value), 240)
            if label and text:
                rows.append({"label": label, "value": text})
        return rows
    if not isinstance(raw, list):
        return []
    for item in raw[:MAX_KEY_VALUES]:
        if not isinstance(item, dict):
            continue
        label = _clip(_safe_text(item.get("label") or item.get("key")), 40)
        value = _clip(_safe_text(item.get("value") or item.get("text")), 240)
        if label and value:
            rows.append({"label": label, "value": value})
    return rows


def _normalize_actions(raw: Any, *, default_copy_text: str) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    seen: set[str] = set()
    source = raw if isinstance(raw, list) else [{"type": "copy"}]
    for item in source:
        if len(actions) >= MAX_ACTIONS:
            break
        if isinstance(item, str):
            action_type = item.strip().lower()
            item = {"type": action_type}
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("type") or "").strip().lower().replace("-", "_")
        if action_type in PRIVILEGED_ACTIONS or action_type not in ALLOWED_ACTIONS:
            continue
        if action_type in seen:
            continue
        if action_type == "copy":
            text = _clip(_safe_text(item.get("text")) or default_copy_text, MAX_TEXT_CHARS)
            if not text:
                continue
            label = _clip(_safe_text(item.get("label")), 24) or "Copy"
            actions.append({"type": "copy", "label": label, "text": text})
            seen.add(action_type)
            continue
        if action_type == "open_url":
            url = _safe_https_url(item.get("url"))
            if not url:
                continue
            label = _clip(_safe_text(item.get("label")), 24) or "Open"
            actions.append({"type": "open_url", "label": label, "url": url})
            seen.add(action_type)
    return actions


def _normalize_group(raw: dict[str, Any]) -> dict[str, Any] | None:
    group_raw = raw.get("group") if isinstance(raw.get("group"), dict) else {}
    group_id = _clip(_safe_text(raw.get("group_id") or group_raw.get("id")), 64)
    if not group_id:
        return None
    index = _coerce_positive_int(raw.get("variant_index") or group_raw.get("index"))
    total = _coerce_positive_int(raw.get("variant_total") or group_raw.get("total"))
    group: dict[str, Any] = {"id": group_id}
    if index:
        group["index"] = index
    if total:
        group["total"] = total
    return group


def _x_search_result_cards(response: dict[str, Any], *, data_path: str) -> list[dict[str, Any]]:
    data = _resolve_data_path(response, data_path)
    if not isinstance(data, list):
        return []
    cards: list[dict[str, Any]] = []
    for index, item in enumerate(data[:MAX_CARDS_PER_BATCH], start=1):
        if not isinstance(item, dict):
            continue
        handle = _safe_text(item.get("author_handle")).lstrip("@")
        excerpt = _clip(_safe_text(item.get("excerpt")), MAX_TEXT_CHARS)
        why = _clip(_safe_text(item.get("why_it_matters")), 400)
        posted_at = _clip(_safe_text(item.get("posted_at")), 80)
        url = _safe_https_url(item.get("post_url") or item.get("url"))
        if not excerpt:
            continue
        title = f"@{handle}" if handle else f"Post {index}"
        sections: list[dict[str, Any]] = [{"type": "quote", "text": excerpt}]
        if why:
            sections.append({"type": "text", "text": why})
        actions: list[dict[str, Any]] = [{"type": "copy", "text": excerpt}]
        if url:
            actions.append({"type": "open_url", "label": "Open", "url": url})
        cards.append(
            {
                "preset": "copy_payload",
                "brand": "x",
                "title": title,
                "subtitle": posted_at or None,
                "body": excerpt,
                "sections": sections,
                "actions": actions,
                "group_id": "x_search_results",
                "variant_index": index,
                "variant_total": min(len(data), MAX_CARDS_PER_BATCH),
            }
        )
    return cards


def _resolve_data_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in [item.strip() for item in path.split(".") if item.strip()]:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _normalize_preset(value: Any) -> str | None:
    preset = str(value or "").strip().lower().replace("-", "_")
    if not preset:
        return None
    return preset if preset in KNOWN_PRESETS else None


def _normalize_brand(value: Any) -> str | None:
    brand = str(value or "").strip().lower()
    if brand in {"twitter", "x.com"}:
        brand = "x"
    if not brand:
        return None
    return brand if brand in KNOWN_BRANDS else "generic"


def _default_title(preset: str | None, brand: str | None) -> str:
    if preset == "social_post" and brand == "x":
        return "X draft"
    if preset == "social_post":
        return "Draft post"
    if preset == "checklist":
        return "Checklist"
    if preset == "option_set":
        return "Options"
    return "Card"


def _coerce_limit(value: Any, *, brand: str | None, preset: str | None) -> int | None:
    number = _coerce_positive_int(value)
    if number:
        return min(number, 10000)
    if preset == "social_post":
        return SOCIAL_CHARACTER_LIMITS.get(brand or "", 280)
    return None


def _coerce_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _character_count(text: str) -> int:
    return len(text)


def _first_text(sections: list[dict[str, Any]]) -> str:
    for section in sections:
        if section.get("type") in {"text", "quote"}:
            return str(section.get("text") or "")
        if section.get("type") == "code":
            return str(section.get("code") or "")
    return ""


def _build_fallback_text(
    *,
    title: str,
    subtitle: str,
    body: str,
    sections: list[dict[str, Any]],
    tags: list[str],
    mentions: list[str],
) -> str:
    lines = [title]
    if subtitle:
        lines.append(subtitle)
    if body:
        lines.append(body)
    else:
        for section in sections:
            section_type = str(section.get("type") or "")
            if section_type in {"text", "quote"}:
                lines.append(str(section.get("text") or ""))
            elif section_type == "code":
                lines.append(str(section.get("code") or ""))
            elif section_type in {"chips", "list"}:
                lines.append(" ".join(str(item) for item in (section.get("items") or [])))
            elif section_type == "key_value":
                for row in section.get("rows") or []:
                    if isinstance(row, dict):
                        lines.append(f"{row.get('label')}: {row.get('value')}")
    extras = [*tags, *mentions]
    if extras:
        lines.append(" ".join(extras))
    return "\n".join(item for item in lines if str(item).strip())


def _safe_https_url(value: Any) -> str | None:
    url = _safe_text(value)
    if not url or len(url) > 500 or any(ch.isspace() for ch in url):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    host = parsed.hostname or ""
    if not host or host in {"localhost", "127.0.0.1"} or host.endswith(".local"):
        return None
    return url


def _string_list(value: Any, *, limit: int, max_item_chars: int = 80) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = _clip(_safe_text(raw), max_item_chars)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _rough_size(value: Any) -> int:
    return len(repr(value))
