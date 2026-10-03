"""Notion API helpers for the gateway.

Every call carries the ``Notion-Version`` header (omitting it fails requests)
and the bearer token resolved from the credential manager. Errors are mapped
onto the same vocabulary the rest of the credentials stack speaks: a 401/403
becomes ``PermissionError`` (the grant was withdrawn on Notion's side — probe
and tool paths turn that into ``reauth_required``), everything else propagates
as its httpx exception.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_NUMBERED_ITEM = re.compile(r"^(\d+)[.)]\s+(.*)$")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# A fetch reads page trees; unbounded children walks have eaten rate budget on
# workspace-sized pages. These ceilings keep one tool call bounded.
_MAX_CHILD_PAGES = 5
_MAX_BLOCKS = 120


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        raise PermissionError(
            "Notion rejected the credential (the connection may have been removed "
            "on Notion's side). Reconnect in Cosmic settings."
        )
    resp.raise_for_status()


async def search(token: str, *, query: str = "", filter_object: str = "", limit: int = 8) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"page_size": max(1, min(int(limit), 20))}
    if query:
        payload["query"] = query
    if filter_object in {"page", "database"}:
        payload["filter"] = {"property": "object", "value": filter_object}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"{NOTION_API_BASE}/search", headers=_headers(token), json=payload)
        _raise_for_status(resp)
        data = resp.json()
    return [item for item in data.get("results", []) if isinstance(item, dict)]


async def get_page(token: str, page_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{NOTION_API_BASE}/pages/{page_id}", headers=_headers(token)
        )
        _raise_for_status(resp)
        return resp.json()


async def get_database(token: str, database_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{NOTION_API_BASE}/databases/{database_id}", headers=_headers(token)
        )
        _raise_for_status(resp)
        return resp.json()


async def get_block_children(token: str, block_id: str) -> list[dict[str, Any]]:
    """Flatten a block subtree into a bounded list of leaf-ish blocks."""
    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(_MAX_CHILD_PAGES):
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{NOTION_API_BASE}/blocks/{block_id}/children",
                headers=_headers(token),
                params=params,
            )
            _raise_for_status(resp)
            data = resp.json()
        for block in data.get("results", []):
            if not isinstance(block, dict):
                continue
            if block.get("has_children") and len(collected) < _MAX_BLOCKS:
                # One level of nesting is what drafts need; deeper subtrees
                # collapse into a marker rather than recursing unbounded.
                collected.append(block)
                try:
                    collected.extend(
                        await get_block_children(token, str(block.get("id")))
                    )
                except Exception:
                    logger.warning("notion_fetch.children_failed block=%s", block.get("id"))
            else:
                collected.append(block)
            if len(collected) >= _MAX_BLOCKS:
                return collected[:_MAX_BLOCKS]
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return collected[:_MAX_BLOCKS]


async def create_page(token: str, *, parent: dict[str, Any], title: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    properties: dict[str, Any]
    if "database_id" in parent:
        # Database children take their title from the schema's title property;
        # "title" is the overwhelming convention and wrong guesses still fail
        # loudly with a 400 the caller can surface.
        properties = {"title": {"title": [{"text": {"content": title}}]}}
    else:
        properties = {"title": {"title": [{"text": {"content": title}}]}}
    payload: dict[str, Any] = {"parent": parent, "properties": properties}
    if blocks:
        # Notion accepts at most 100 blocks per create call; pages longer than
        # that are created empty then appended in slices.
        payload["children"] = blocks[:100]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{NOTION_API_BASE}/pages", headers=_headers(token), json=payload)
        _raise_for_status(resp)
        page = resp.json()
    if len(blocks) > 100:
        page_id = str(page.get("id") or "")
        if page_id:
            await append_blocks(token, page_id, blocks[100:])
    return page


async def append_blocks(token: str, block_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"{NOTION_API_BASE}/blocks/{block_id}/children",
            headers=_headers(token),
            json={"children": blocks[:100]},
        )
        _raise_for_status(resp)
        return resp.json()


async def update_page_title(token: str, page_id: str, title: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"{NOTION_API_BASE}/pages/{page_id}",
            headers=_headers(token),
            json={"properties": {"title": {"title": [{"text": {"content": title}}]}}},
        )
        _raise_for_status(resp)
        return resp.json()


def extract_notion_id(reference: str) -> str:
    """Accept a bare id (with or without dashes) or a Notion URL.

    Page URLs carry a 32-hex tail: ``...-8ba5a435b7c2460fa8e0f2e2a0b6c9d1`` or
    ``8ba5a435b7c2460fa8e0f2e2a0b6c9d1`` as a vanity path segment. The id is
    always the last 32 characters of the final path segment; hex-filtering the
    whole URL would happily harvest hex-looking letters from the slug.
    """
    text = str(reference or "").strip()
    if not text:
        return ""
    if "notion.so" in text or "notion.site" in text:
        path = text.split("?", 1)[0].split("#", 1)[0]
        segment = path.rstrip("/").rsplit("/", 1)[-1]
        compact = segment.replace("-", "")
        if len(compact) >= 32:
            return compact[-32:].lower()
        return ""
    compact = text.replace("-", "")
    if len(compact) == 32:
        return compact.lower()
    return ""


def _rich_text(text: str) -> list[dict[str, Any]]:
    # API-shaped rich text objects (plain_text included) so any consumer of a
    # constructed block reads them the same way it reads real Notion payloads.
    text = text or ""
    chunks = [text[index : index + 1900] for index in range(0, len(text), 1900)] or [""]
    return [
        {"type": "text", "text": {"content": chunk}, "plain_text": chunk}
        for chunk in chunks
    ]


def markdown_to_notion_blocks(content: str) -> list[dict[str, Any]]:
    """Turn the model's markdown-ish draft into Notion blocks.

    Supported: headings (#..####), bullets (- / *), numbered lists, to-dos
    (- [ ] / - [x]), quotes (>), fenced code, and plain paragraphs. Blank
    lines separate blocks; anything unrecognized stays a paragraph, so no
    line of the draft is ever silently dropped.
    """
    blocks: list[dict[str, Any]] = []
    lines = str(content or "").replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if stripped.startswith("```"):
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            index += 1  # closing fence
            blocks.append(
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": _rich_text("\n".join(code_lines)),
                        "language": "plain text",
                    },
                }
            )
            continue
        if stripped.startswith("#### "):
            blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rich_text(stripped[5:])}})
        elif stripped.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": _rich_text(stripped[4:])}})
        elif stripped.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": _rich_text(stripped[3:])}})
        elif stripped.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": _rich_text(stripped[2:])}})
        elif stripped.startswith("> "):
            blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": _rich_text(stripped[2:])}})
        elif stripped.startswith("- [ ] ") or stripped.startswith("* [ ] "):
            blocks.append({"object": "block", "type": "to_do", "to_do": {"rich_text": _rich_text(stripped[6:]), "checked": False}})
        elif stripped.startswith("- [x] ") or stripped.startswith("* [x] "):
            blocks.append({"object": "block", "type": "to_do", "to_do": {"rich_text": _rich_text(stripped[6:]), "checked": True}})
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rich_text(stripped[2:])}})
        else:
            numbered = _NUMBERED_ITEM.match(stripped)
            if numbered:
                # Notion renumbers list items on render, so the draft's literal
                # number is dropped rather than frozen into the text.
                blocks.append({"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": _rich_text(numbered.group(2))}})
            else:
                blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(stripped)}})
        index += 1
    return blocks


def page_title(page: dict[str, Any]) -> str:
    """Pull a human title out of a page/database payload across layouts."""
    properties = page.get("properties") if isinstance(page.get("properties"), dict) else {}
    candidates: list[Any] = []
    for value in properties.values():
        if isinstance(value, dict) and value.get("type") == "title":
            candidates.append(value.get("title"))
    if page.get("object") == "database":
        candidates.append(page.get("title"))
    for candidate in candidates:
        if isinstance(candidate, list):
            parts = [
                str(item.get("plain_text") or "")
                for item in candidate
                if isinstance(item, dict)
            ]
            title = "".join(parts).strip()
            if title:
                return title
    return ""


def page_url(item: dict[str, Any]) -> str:
    return str(item.get("url") or "").strip()


def result_summary(item: dict[str, Any]) -> dict[str, Any]:
    """The normalized row a search/fetch result hands back to the model."""
    object_type = str(item.get("object") or "").strip()
    return {
        "id": str(item.get("id") or ""),
        "object": object_type,
        "title": page_title(item) or "Untitled",
        "url": page_url(item),
        "last_edited_at": str(item.get("last_edited_time") or ""),
        "parent": item.get("parent") if isinstance(item.get("parent"), dict) else {},
    }


def block_to_text(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "")
    body = block.get(block_type) if isinstance(block.get(block_type), dict) else {}
    rich = body.get("rich_text") if isinstance(body.get("rich_text"), list) else []
    text = "".join(
        str(item.get("plain_text") or "") for item in rich if isinstance(item, dict)
    )
    if block_type.startswith("heading_"):
        level = block_type.split("_")[1]
        return f"{'#' * int(level)} {text}"
    if block_type == "bulleted_list_item":
        return f"- {text}"
    if block_type == "numbered_list_item":
        return f"1. {text}"
    if block_type == "to_do":
        return f"- [{'x' if body.get('checked') else ' '}] {text}"
    if block_type == "quote":
        return f"> {text}"
    if block_type == "code":
        return f"```\n{text}\n```"
    if block_type in ("divider",):
        return "---"
    return text
