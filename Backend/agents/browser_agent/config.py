"""Browser Agent configuration for the COSMIC adapter around cosmic-browser-use."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = ["AGENT_ROOT", "BACKEND_ROOT", "BrowserAgentConfig"]

# Env layering mirrors the Slide Agent: process environment (systemd
# EnvironmentFile) wins over agent.env, which wins over Backend/.env.
load_dotenv(AGENT_ROOT / "agent.env")
load_dotenv(BACKEND_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


@dataclass(slots=True)
class BrowserAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""

    # Checkout of the cosmic-browser-use repo (sibling of the Cosmic-OS repo on
    # the VM, exactly like the cosmic-memory checkout). Contains main.py and
    # the browser_memory package; its directory is prepended to sys.path.
    browser_use_home: Path = BACKEND_ROOT.parent.parent / "cosmic-browser-use" / "cosmic-browser-use"

    headless: bool = True
    default_max_steps: int = 40
    # Step-budget extension policy. A run that reaches its ceiling asks
    # whether to continue instead of stopping mid-thought; these bound what it
    # can be granted. Set extensions to 0 to restore a hard stop.
    default_max_step_extensions: int = 2
    default_step_extension_size: int = 15
    default_max_total_steps: int = 90
    run_timeout_sec: int = 840
    # Budget for one AskUser interrupt round trip to the desktop (OTP, CAPTCHA,
    # phone-approval, ambiguous forms). Longer than cosmic-browser-use's own
    # 120s stdin-input default since a human now has to notice a desktop card
    # instead of just typing at a terminal.
    ask_user_wait_sec: int = 240

    working_dir_root: Path = BACKEND_ROOT
    artifacts_root: Path = BACKEND_ROOT / "runs" / "artifacts"

    # Session recall ledger (browser.recall_session) — mirrors the Firecrawl
    # specialist's own session-runs store exactly (agents/firecrawl_web_scrape
    # /store/data/firecrawl_session_runs.db): a small SQLite index of what was
    # asked/found per run, so the orchestrator can look up prior browser work
    # without launching a browser. Does not duplicate cosmic-browser-use's own
    # per-run logs/checkpoints — it just indexes and points at them.
    store_root: Path = AGENT_ROOT / "store"
    session_db_path: Path = AGENT_ROOT / "store" / "data" / "browser_session_runs.db"

    @classmethod
    def from_env(cls) -> "BrowserAgentConfig":
        default_home = BACKEND_ROOT.parent.parent / "cosmic-browser-use" / "cosmic-browser-use"
        home = Path(_first_env("BROWSER_USE_HOME", default=str(default_home)))
        store_root = Path(_first_env("BROWSER_AGENT_STORE_ROOT", default=str(AGENT_ROOT / "store")))
        return cls(
            redis_url=_first_env("REDIS_URL", default="redis://127.0.0.1:6379/0"),
            gateway_url=_first_env("GATEWAY_URL", default="http://127.0.0.1:8080"),
            gateway_internal_token=_first_env("GATEWAY_INTERNAL_TOKEN", default=""),
            browser_use_home=home,
            headless=_env_bool("BROWSER_AGENT_HEADLESS", True),
            default_max_steps=max(1, _env_int("BROWSER_AGENT_MAX_STEPS", 40)),
            default_max_step_extensions=max(0, _env_int("BROWSER_AGENT_MAX_STEP_EXTENSIONS", 2)),
            default_step_extension_size=max(0, _env_int("BROWSER_AGENT_STEP_EXTENSION_SIZE", 15)),
            default_max_total_steps=max(1, _env_int("BROWSER_AGENT_MAX_TOTAL_STEPS", 90)),
            run_timeout_sec=max(30, _env_int("BROWSER_AGENT_RUN_TIMEOUT_SEC", 840)),
            ask_user_wait_sec=max(30, _env_int("BROWSER_AGENT_ASK_USER_TIMEOUT_SEC", 240)),
            working_dir_root=Path(_first_env("BROWSER_AGENT_WORKING_DIR_ROOT", default=str(BACKEND_ROOT))),
            artifacts_root=Path(_first_env("BROWSER_AGENT_ARTIFACTS_ROOT", default=str(BACKEND_ROOT / "runs" / "artifacts"))),
            store_root=store_root,
            session_db_path=store_root / "data" / "browser_session_runs.db",
        )

    def ensure_import_path(self) -> Path:
        """Make the cosmic-browser-use checkout importable.

        Returns the resolved home directory (the one containing main.py), so
        callers can produce precise errors when the checkout is missing.
        """
        import sys

        home = self.browser_use_home.expanduser().resolve()
        if not (home / "main.py").is_file():
            raise RuntimeError(
                f"cosmic-browser-use checkout not found at {home}. "
                "Set BROWSER_USE_HOME to the directory containing main.py "
                "(bootstrap clones it as a sibling of the Cosmic-OS repo)."
            )
        if str(home) not in sys.path:
            sys.path.insert(0, str(home))
        return home
