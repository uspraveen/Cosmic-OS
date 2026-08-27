from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parent


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _default_alpha_root() -> Path:
    if os.name == "nt":
        return AGENT_ROOT / "runtime" / "alpha"
    return Path("/var/lib/cosmic/alpha")


def _optional_path(value: str | None) -> Path | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    return Path(normalized).expanduser()


@dataclass(frozen=True)
class AlphaAgentConfig:
    redis_url: str
    gateway_url: str
    gateway_internal_token: str
    orchestrator_url: str
    orchestrator_internal_token: str
    enabled: bool
    alpha_root: Path
    project_db_path: Path
    docker_image: str
    docker_network: str
    docker_memory: str
    docker_cpus: str
    docker_pids_limit: int
    docker_timeout_sec: float
    allow_docker_smoke: bool
    codex_home: Path
    cursor_home: Path
    opencode_home: Path
    codex_sandbox: str
    codex_timeout_sec: float
    codex_default_model: str
    cursor_timeout_sec: float
    cursor_default_model: str
    cursor_init_timeout_sec: float
    opencode_timeout_sec: float
    opencode_default_model: str
    zen_api_key: str
    cli_idle_check_sec: float

    @classmethod
    def from_env(cls) -> "AlphaAgentConfig":
        alpha_root = _optional_path(os.getenv("ALPHA_WORKSPACE_ROOT")) or _default_alpha_root()
        project_db_path = (
            _optional_path(os.getenv("ALPHA_PROJECT_DB_PATH"))
            or AGENT_ROOT
            / "store"
            / "data"
            / "projects.db"
        )
        codex_sandbox = os.getenv("ALPHA_CODEX_SANDBOX", "danger-full-access").strip()
        if codex_sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            codex_sandbox = "danger-full-access"
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            orchestrator_url=os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8743").strip(),
            orchestrator_internal_token=(
                os.getenv("ORCHESTRATOR_INTERNAL_TOKEN")
                or os.getenv("GATEWAY_INTERNAL_TOKEN")
                or ""
            ).strip(),
            enabled=_truthy(os.getenv("ALPHA_AGENT_ENABLED"), default=False),
            alpha_root=alpha_root,
            project_db_path=project_db_path,
            docker_image=os.getenv("ALPHA_DOCKER_IMAGE", "ubuntu:24.04").strip()
            or "ubuntu:24.04",
            docker_network=os.getenv("ALPHA_DOCKER_NETWORK", "bridge").strip() or "bridge",
            docker_memory=os.getenv("ALPHA_DOCKER_MEMORY", "4g").strip() or "4g",
            docker_cpus=os.getenv("ALPHA_DOCKER_CPUS", "2").strip() or "2",
            docker_pids_limit=max(64, int(os.getenv("ALPHA_DOCKER_PIDS_LIMIT", "512") or "512")),
            docker_timeout_sec=max(
                1.0,
                float(os.getenv("ALPHA_DOCKER_TIMEOUT_SEC", "300") or "300"),
            ),
            allow_docker_smoke=_truthy(os.getenv("ALPHA_ALLOW_DOCKER_SMOKE"), default=False),
            codex_home=(
                _optional_path(os.getenv("ALPHA_CODEX_HOME"))
                or alpha_root
                / "homes"
                / "codex"
            ),
            cursor_home=(
                _optional_path(os.getenv("ALPHA_CURSOR_HOME"))
                or alpha_root
                / "homes"
                / "cursor"
            ),
            opencode_home=(
                _optional_path(os.getenv("ALPHA_OPENCODE_HOME"))
                or alpha_root
                / "homes"
                / "opencode"
            ),
            codex_sandbox=codex_sandbox,
            codex_timeout_sec=max(
                30.0,
                float(os.getenv("ALPHA_CODEX_TIMEOUT_SEC", "14400") or "14400"),
            ),
            codex_default_model=os.getenv("ALPHA_CODEX_MODEL", "").strip(),
            cursor_timeout_sec=max(
                30.0,
                float(os.getenv("ALPHA_CURSOR_TIMEOUT_SEC", "14400") or "14400"),
            ),
            cursor_default_model=os.getenv("ALPHA_CURSOR_MODEL", "").strip(),
            cursor_init_timeout_sec=max(
                30.0,
                float(os.getenv("ALPHA_CURSOR_INIT_TIMEOUT_SEC", "180") or "180"),
            ),
            opencode_timeout_sec=max(
                30.0,
                float(os.getenv("ALPHA_OPENCODE_TIMEOUT_SEC", "14400") or "14400"),
            ),
            # Zen model ids move weekly; this is only the pre-settings fallback.
            opencode_default_model=os.getenv("ALPHA_OPENCODE_MODEL", "mimo-v2.5-free").strip()
            or "mimo-v2.5-free",
            zen_api_key=(os.getenv("ALPHA_OPENCODE_ZEN_API_KEY") or "").strip(),
            cli_idle_check_sec=max(
                30.0,
                float(os.getenv("ALPHA_CLI_IDLE_CHECK_SEC", "60") or "60"),
            ),
        )
