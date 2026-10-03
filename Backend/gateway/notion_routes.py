"""Notion tool routes for the orchestrator.

- POST /internal/notion/search        → search pages/databases the user shared (direct read)
- POST /internal/notion/fetch         → read one page/database as text (direct read)
- POST /internal/notion/pages/propose → park a create/update behind an approval card

Reads execute immediately because they can only ever see the pages the user
shared at connect time. Writes never touch the Notion API here: propose stores
the exact payload, publishes the approval card, and only the desktop Approve
button (POST /channels/notion/approvals/{id}/approve) performs the write.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import notion_client
from .credentials.routes import _get_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["notion"])


class NotionAccountRef(BaseModel):
    account_id: str | None = None
    account_hint: str | None = Field(default=None, description="Workspace name to disambiguate multiple connected workspaces.")


class NotionSearchRequest(NotionAccountRef):
    query: str = ""
    filter: str = Field(default="", description="Restrict to 'page' or 'database'. Empty searches both.")
    limit: int = 8


class NotionFetchRequest(NotionAccountRef):
    page: str = Field(description="Page or database id, or a Notion URL.")


class NotionProposeRequest(NotionAccountRef):
    operation: str = Field(description="'create_page' or 'update_page'.")
    parent_page_id: str = ""
    parent_database_id: str = ""
    parent_title: str = ""
    page_id: str = ""
    title: str = ""
    content: str = ""
    append: bool = True
    session_id: str | None = None
    task_id: str | None = None
    channel: str | None = None
    purpose: str | None = None


def _check_internal_token(request: Request) -> None:
    # Reuse the credentials routes' internal-token check: same secret, same
    # caller class (orchestrator on the VM).
    from .credentials.routes import _check_internal_token as check

    check(request)


async def _resolve_notion_credential(request: Request, body: NotionAccountRef, *, operation_mode: str) -> dict[str, Any]:
    mgr = _get_manager(request)
    resolved = await mgr.resolve_credential(
        provider="notion",
        required_scopes=[],
        account_id=str(body.account_id or "").strip() or None,
        account_hint=str(body.account_hint or "").strip() or None,
        allow_primary_fallback=True,
        operation_mode=operation_mode,
    )
    if resolved:
        return resolved
    accounts = [acct for acct in mgr.list_accounts("notion") if acct.get("status") == "active"]
    if not accounts:
        raise HTTPException(
            status_code=404,
            detail="No Notion workspace is connected. Connect one in Settings → Integrations → Notion.",
        )
    raise HTTPException(
        status_code=409,
        detail={
            "message": (
                "Multiple Notion workspaces are connected. Name the workspace "
                "(account_hint) or pass its account_id."
            ),
            "candidates": [
                {
                    "account_id": acct.get("account_id"),
                    "workspace": acct.get("account_display_label") or acct.get("display_name"),
                }
                for acct in accounts[:8]
            ],
        },
    )


@router.post("/internal/notion/search")
async def notion_search(body: NotionSearchRequest, request: Request):
    _check_internal_token(request)
    resolved = await _resolve_notion_credential(request, body, operation_mode="read")
    token = str(resolved.get("access_token") or "")
    try:
        results = await notion_client.search(
            token,
            query=str(body.query or "").strip(),
            filter_object=str(body.filter or "").strip().lower(),
            limit=body.limit,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("notion_search failed")
        raise HTTPException(status_code=502, detail=f"Notion search failed: {exc}") from exc
    return {
        "status": "ok",
        "workspace": resolved.get("account_label") or resolved.get("account_display_name"),
        "account_id": resolved.get("account_id"),
        "results": [notion_client.result_summary(item) for item in results],
    }


async def _load_target(token: str, reference: str) -> dict[str, Any]:
    import httpx

    object_id = notion_client.extract_notion_id(reference)
    if not object_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not read a Notion page/database id from that reference. "
                "Search first with notion_search and pass the id or URL of a result."
            ),
        )
    try:
        return await notion_client.get_page(token, object_id)
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code != 404:
            raise HTTPException(status_code=502, detail=f"Notion lookup failed: {exc}") from exc
    try:
        return await notion_client.get_database(token, object_id)
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                "Notion has no page or database with that id among the shared pages. "
                "Search with notion_search and use a result's id or URL."
            ),
        ) from exc


@router.post("/internal/notion/fetch")
async def notion_fetch(body: NotionFetchRequest, request: Request):
    _check_internal_token(request)
    resolved = await _resolve_notion_credential(request, body, operation_mode="read")
    token = str(resolved.get("access_token") or "")
    target = await _load_target(token, body.page)
    object_id = str(target.get("id") or "")
    object_type = str(target.get("object") or "page")
    blocks: list[dict[str, Any]] = []
    try:
        blocks = await notion_client.get_block_children(token, object_id)
    except PermissionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("notion_fetch.children_failed id=%s error=%s", object_id, str(exc)[:200])
    content_text = "\n".join(
        line for line in (notion_client.block_to_text(block) for block in blocks) if line
    ).strip()
    return {
        "status": "ok",
        "workspace": resolved.get("account_label") or resolved.get("account_display_name"),
        "account_id": resolved.get("account_id"),
        "page": {
            **notion_client.result_summary(target),
            "object": object_type,
        },
        "content_text": content_text,
        "block_count": len(blocks),
    }


@router.post("/internal/notion/pages/propose")
async def notion_pages_propose(body: NotionProposeRequest, request: Request):
    _check_internal_token(request)
    operation = str(body.operation or "").strip()
    if operation not in {"create_page", "update_page"}:
        raise HTTPException(status_code=400, detail="operation must be 'create_page' or 'update_page'.")
    title = str(body.title or "").strip()
    content = str(body.content or "")
    if operation == "create_page" and not title:
        raise HTTPException(status_code=400, detail="title is required to create a Notion page.")
    if operation == "update_page":
        if not str(body.page_id or "").strip():
            raise HTTPException(
                status_code=400,
                detail="page_id is required to update a page. Search first with notion_search.",
            )
        if not content and not title:
            raise HTTPException(status_code=400, detail="Provide content to append and/or a new title.")

    resolved = await _resolve_notion_credential(request, body, operation_mode="write")
    token = str(resolved.get("access_token") or "")

    parent_title = str(body.parent_title or "").strip()
    parent_page_id = notion_client.extract_notion_id(str(body.parent_page_id or ""))
    parent_database_id = notion_client.extract_notion_id(str(body.parent_database_id or ""))
    page_id = notion_client.extract_notion_id(str(body.page_id or ""))
    if operation == "create_page" and not parent_page_id and not parent_database_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "parent_page_id or parent_database_id is required — the user chooses where "
                "the page lands. Resolve it with notion_search first."
            ),
        )
    if operation == "update_page" and not page_id:
        raise HTTPException(
            status_code=400,
            detail="Could not read a page id from that reference. Use an id from notion_search or notion_fetch.",
        )

    # Best-effort display names for the card. Failures never block the
    # proposal; the card falls back to the workspace name.
    card_parent = parent_title
    card_page_title = ""
    try:
        if operation == "create_page" and (parent_page_id or parent_database_id):
            target = await notion_client.get_page(token, parent_page_id) if parent_page_id else await notion_client.get_database(token, parent_database_id)
            card_parent = notion_client.page_title(target) or card_parent or "your workspace"
        if operation == "update_page" and page_id:
            target = await notion_client.get_page(token, page_id)
            card_page_title = notion_client.page_title(target)
    except Exception as exc:
        logger.info("notion_propose.display_lookup_failed error=%s", str(exc)[:200])

    pending, _created = request.app.state.gateway_runtime.create_notion_pending_and_notify(
        {
            "operation": operation,
            "account_id": str(resolved.get("account_id") or ""),
            "workspace": str(resolved.get("account_label") or resolved.get("account_display_name") or ""),
            "parent_page_id": parent_page_id,
            "parent_database_id": parent_database_id,
            "parent_title": card_parent,
            "page_id": page_id,
            "page_title": title or card_page_title,
            "content_text": content,
            "append": bool(body.append),
            "session_id": body.session_id,
            "task_id": body.task_id,
            "channel": body.channel,
            "purpose": body.purpose,
        }
    )
    destination = (
        f"new page in {pending.get('parent_title') or 'your workspace'}"
        if operation == "create_page"
        else f"update to {pending.get('page_title') or pending.get('parent_title') or 'the page'}"
    )
    return {
        "status": "approval_required",
        "approval_id": pending.get("approval_id"),
        "operation": operation,
        "workspace": pending.get("workspace"),
        "destination": destination,
        "reason": "Notion writes wait for an explicit approval, like outgoing email.",
    }
