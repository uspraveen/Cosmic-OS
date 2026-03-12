from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / "orchestrator.env")
load_dotenv(BACKEND_ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class OrchestratorConfig:
    host: str = "127.0.0.1"
    port: int = 8743
    internal_token: str = ""
    signing_secret: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-6"
    anthropic_version: str = "2023-06-01"
    max_tokens: int = 16000
    request_timeout_sec: float = 300.0
    redis_url: str = ""
    task_input_requests_stream: str = "user_input:requests"
    task_input_replies_stream: str = "user_input:replies"
    task_input_orchestrator_group: str = "orchestrator"
    task_ledger_db_path: Path = BACKEND_ROOT / "agents" / "orchestrator" / "store" / "data" / "task_ledger.db"

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        return cls(
            host=os.getenv("ORCHESTRATOR_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_env_int("ORCHESTRATOR_PORT", 8743),
            internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            signing_secret=os.getenv("GATEWAY_SIGNING_SECRET", "").strip(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6").strip(),
            anthropic_version=os.getenv("ANTHROPIC_VERSION", "2023-06-01").strip(),
            max_tokens=max(256, _env_int("OPUS_MAX_TOKENS", 16000)),
            request_timeout_sec=max(30.0, _env_float("ORCHESTRATOR_REQUEST_TIMEOUT_SEC", 300.0)),
            redis_url=os.getenv("REDIS_URL", "").strip(),
            task_input_requests_stream=os.getenv("TASK_INPUT_REQUESTS_STREAM", "user_input:requests").strip()
            or "user_input:requests",
            task_input_replies_stream=os.getenv("TASK_INPUT_REPLIES_STREAM", "user_input:replies").strip()
            or "user_input:replies",
            task_input_orchestrator_group=os.getenv("TASK_INPUT_ORCHESTRATOR_GROUP", "orchestrator").strip()
            or "orchestrator",
            task_ledger_db_path=Path(
                os.getenv(
                    "ORCHESTRATOR_TASK_LEDGER_DB_PATH",
                    str(BACKEND_ROOT / "agents" / "orchestrator" / "store" / "data" / "task_ledger.db"),
                )
            ).expanduser(),
        )
