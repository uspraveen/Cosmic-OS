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


def generate_event_id(prefix: str = "evt") -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def validate_outbound_version(contract_version: str) -> None:
    if contract_version != CURRENT_WRITE_VERSION:
        raise ValueError(
            f"Outbound contract version {contract_version!r} does not match current write version {CURRENT_WRITE_VERSION!r}."
        )


ArtifactKind = Literal["input", "output", "intermediate"]
ArtifactAudience = Literal["deliverable", "supporting", "debug"]
EventType = Literal[
    "task.accepted",
    "task.progress",
    "task.suspended",
    "task.resumed",
    "task.deferred",
    "artifact.added",
    "task.completed",
    "task.failed",
    "task.cancelled",
    "task.dlq",
    "task.rejected",
]

TERMINAL_EVENTS: set[EventType] = {"task.completed", "task.failed", "task.cancelled", "task.dlq"}
NON_TERMINAL_EVENTS: set[EventType] = {
    "task.accepted",
    "task.progress",
    "task.suspended",
    "task.resumed",
    "task.deferred",
    "artifact.added",
    "task.rejected",
}


class ArtifactManifest(BaseModel):
    artifact_id: str
    task_id: str
    mime: str
    sha256: str
    path: str
    source_url: str | None = None
    created_by_agent: str
    created_at: datetime = Field(default_factory=utcnow)
    kind: ArtifactKind = "output"
    audience: ArtifactAudience = "deliverable"


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


class EventEnvelope(BaseModel):
    task_id: str
    agent_id: str
    event_type: EventType
    seq: int
    payload: dict[str, Any] = Field(default_factory=dict)
    emitted_at: datetime = Field(default_factory=utcnow)
    contract_version: str = CURRENT_WRITE_VERSION

    @field_validator("contract_version")
    @classmethod
    def check_contract_version(cls, value: str) -> str:
        if value not in ACCEPTED_READ_VERSIONS:
            raise ValueError(
                f"Unacceptable contract version: {value}. Accepted: {sorted(ACCEPTED_READ_VERSIONS)}"
            )
        return value

    @field_validator("seq")
    @classmethod
    def check_seq(cls, value: int) -> int:
        if value < 1:
            raise ValueError("EventEnvelope.seq must be >= 1")
        return value


class AgentError(BaseModel):
    code: str
    retryable: bool
    message: str
    next_action: str | None = None


class AgentResult(BaseModel):
    status: Literal["completed", "failed"]
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactManifest] = Field(default_factory=list)
    error: AgentError | None = None


class TaskInProgress(BaseModel):
    task_id: str
    idempotency_key: str
    executing_since: datetime
    check_after_sec: int


class Heartbeat(BaseModel):
    agent_id: str
    instance_id: str
    healthy: bool
    current_load: int
    max_concurrency: int
    heartbeat_ttl_sec: int
    last_seen: datetime = Field(default_factory=utcnow)

    @field_validator("current_load", "max_concurrency", "heartbeat_ttl_sec")
    @classmethod
    def check_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Heartbeat numeric fields must be >= 0")
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
