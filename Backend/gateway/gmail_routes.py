from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from .runtime import GatewayRuntime

logger = logging.getLogger(__name__)
router = APIRouter(tags=["gmail"])


@router.post("/webhooks/gmail/pubsub")
async def gmail_pubsub_webhook(
    request: Request,
    secret: str = Query("", alias="secret"),
):
    runtime: GatewayRuntime = request.app.state.gateway_runtime
    configured_secret = str(runtime.config.gmail_webhook_secret or "").strip()
    supplied_secret = (
        str(secret or "").strip()
        or str(request.headers.get("X-Cosmic-Gmail-Webhook-Secret") or "").strip()
    )
    if configured_secret and not supplied_secret:
        raise HTTPException(status_code=401, detail="Missing Gmail webhook secret.")
    if configured_secret and not _constantish_equal(configured_secret, supplied_secret):
        raise HTTPException(status_code=403, detail="Invalid Gmail webhook secret.")

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
    message = payload.get("message") if isinstance(payload, dict) else {}
    if not isinstance(message, dict):
        raise HTTPException(status_code=400, detail="Missing Pub/Sub message.")
    decoded = _decode_pubsub_data(str(message.get("data") or ""))
    email_address = str(decoded.get("emailAddress") or "").strip()
    history_id = str(decoded.get("historyId") or "").strip()
    if not email_address or not history_id:
        return {
            "status": "ignored",
            "reason": "missing_gmail_email_or_history_id",
        }
    raw_payload = {
        "pubsub_message_id": str(message.get("messageId") or ""),
        "publish_time": str(message.get("publishTime") or ""),
        "attributes": message.get("attributes") if isinstance(message.get("attributes"), dict) else {},
        "decoded": decoded,
    }
    asyncio.create_task(
        _process_gmail_pubsub(
            runtime,
            email_address=email_address,
            history_id=history_id,
            raw_payload=raw_payload,
        )
    )
    return {"status": "accepted", "email_address": email_address, "history_id": history_id}


def _decode_pubsub_data(value: str) -> dict[str, Any]:
    if not value:
        return {}
    padded = value + ("=" * ((4 - len(value) % 4) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except Exception:
        logger.exception("gateway.gmail_pubsub_decode_failed")
        return {}
    return payload if isinstance(payload, dict) else {}


def _constantish_equal(left: str, right: str) -> bool:
    return bool(left) and hmac.compare_digest(left, right)


async def _process_gmail_pubsub(
    runtime: GatewayRuntime,
    *,
    email_address: str,
    history_id: str,
    raw_payload: dict[str, Any],
) -> None:
    try:
        await runtime.handle_gmail_pubsub_notification(
            email_address=email_address,
            history_id=history_id,
            raw_payload=raw_payload,
        )
    except Exception:
        logger.exception(
            "gateway.gmail_pubsub_background_failed email=%s history_id=%s",
            email_address,
            history_id,
        )
