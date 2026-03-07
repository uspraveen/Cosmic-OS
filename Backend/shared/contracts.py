from __future__ import annotations

import hmac
import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

CURRENT_WRITE_VERSION = "1.0"
ACCEPTED_READ_VERSIONS = {CURRENT_WRITE_VERSION}

SOURCE_PRIORITY_MAP: dict[str, Literal["high", "normal", "low"]] = {
    "user": "high",
    "webhook": "normal",
    "hook": "normal",
    "agent": "normal",
    "cron": "low",
    "heartbeat": "low",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_task_id() -> str:
    return f"tsk_{uuid4().hex[:12]}"


class TaskEnvelope(BaseModel):
    task_id: str
    task_list_id: str
    parent_task_id: str | None = None
    session_id: str | None = None
    sender: str
    recipient: str
    intent: str
    input: dict[str, Any]
    input_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    idempotency_key: str
    deadline_ts: datetime | None = None
    priority: Literal["high", "normal", "low"] = "normal"
    leader_epoch: int | None = None
    signature: str
    contract_version: str = CURRENT_WRITE_VERSION
    created_at: datetime = Field(default_factory=utcnow)
    source: Literal["user", "cron", "webhook", "heartbeat", "hook", "agent"] = "user"
    source_id: str | None = None
    channel: str | None = None

    @field_validator("contract_version")
    @classmethod
    def check_contract_version(cls, value: str) -> str:
        if value not in ACCEPTED_READ_VERSIONS:
            raise ValueError(
                f"Unacceptable contract version: {value}. Accepted: {sorted(ACCEPTED_READ_VERSIONS)}"
            )
        return value


def _canonical_payload(task: TaskEnvelope | dict[str, Any]) -> bytes:
    if isinstance(task, TaskEnvelope):
        payload = task.model_dump(mode="json")
    else:
        payload = dict(task)
    payload.pop("signature", None)
    canonical_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return canonical_json.encode("utf-8")


def sign_task_envelope(task: TaskEnvelope | dict[str, Any], secret: str) -> str:
    if not secret:
        raise ValueError("Signing secret is required to sign a TaskEnvelope")
    return hmac.new(secret.encode("utf-8"), _canonical_payload(task), "sha256").hexdigest()


def verify_task_envelope(task: TaskEnvelope | dict[str, Any], secret: str) -> bool:
    if not secret:
        return False
    payload = task if isinstance(task, dict) else task.model_dump(mode="json")
    signature = str(payload.get("signature") or "").strip()
    if not signature:
        return False
    expected = sign_task_envelope(payload, secret)
    return hmac.compare_digest(signature, expected)
