#!/usr/bin/env python3
"""COSMIC restart control: activity-aware, graceful fleet restarts.

Replaces the blind `needrestart` restarts that killed in-flight work (the
Sep 18 incident: a libsqlite3 security upgrade bounced all 18 cosmic services
mid-Alpha-build and mid-conversation).

Three entry points:

  agent    Timer entry point (root, every 15 min). If any cosmic service is
           running stale libraries, evaluate the activity gate: restart now
           when idle, defer when busy, force a graceful restart after too many
           deferrals. Never acts while a cron is about to fire -- crons are
           predictable and cheap to wait for.
  restart  Graceful fleet restart right now: raise the drain flag (webhook
           intake starts returning 503 + Retry-After so push senders retry),
           let in-flight work settle, restart the fleet, wait for gateway
           health, clear the flag.
  status   Dry run: print the activity snapshot and the decision as JSON.

Stdlib only, so root can run it with the system python. All busy signals are
recency-based, never status-based -- task_notebooks rows say "running" for
hours after their process died (the zombie lesson).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BACKEND_DIR = "/home/ubuntu/Cosmic-OS/Backend"
DEFAULT_SERVICES = (
    "cosmic-model-router",
    "cosmic-orchestrator",
    "cosmic-gateway",
    "cosmic-memory",
    "cosmic-alpha-agent",
    "cosmic-browser-agent",
    "cosmic-calendar-agent",
    "cosmic-diagram-agent",
    "cosmic-docs-parser-agent",
    "cosmic-email-agent",
    "cosmic-firecrawl-web-scrape-agent",
    "cosmic-gmail-agent",
    "cosmic-google-docs-agent",
    "cosmic-google-sheets-agent",
    "cosmic-image-generator-agent",
    "cosmic-map-agent",
    "cosmic-slide-agent",
    "cosmic-tabular-agent",
    "cosmic-whatsapp-bridge",
    "cosmic-x-twitter-search-agent",
)

DRAIN_FLAG_PATH = "/run/cosmic-drain"
AGENT_STATE_DIR = "/var/lib/cosmic/restart-agent"
DPKG_STATUS = "/var/lib/dpkg/status"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


class Config:
    def __init__(self) -> None:
        self.backend_dir = Path(os.getenv("COSMIC_BACKEND_DIR", DEFAULT_BACKEND_DIR))
        self.services = tuple(
            s
            for s in os.getenv("COSMIC_SERVICES", " ".join(DEFAULT_SERVICES)).split()
            if s
        )
        self.drain_flag = Path(os.getenv("COSMIC_DRAIN_FLAG", DRAIN_FLAG_PATH))
        self.state_path = Path(
            os.getenv(
                "COSMIC_AGENT_STATE",
                str(Path(AGENT_STATE_DIR) / "state.json"),
            )
        )
        self.health_url = os.getenv(
            "COSMIC_GATEWAY_HEALTH_URL", "http://127.0.0.1:8080/health"
        )
        # Seconds of cron margin: never restart this close to a scheduled fire.
        self.cron_margin_sec = _env_int("COSMIC_RESTART_CRON_MARGIN_SEC", 600)
        # Recency windows per busy signal. Each is deliberately generous: the
        # cost of deferring one 15-minute tick is nothing; the cost of killing
        # live work is the incident we are preventing.
        self.busy_message_sec = _env_int("COSMIC_BUSY_MESSAGE_SEC", 120)
        self.busy_notebook_sec = _env_int("COSMIC_BUSY_NOTEBOOK_SEC", 600)
        self.busy_ledger_sec = _env_int("COSMIC_BUSY_LEDGER_SEC", 900)
        # After this many consecutive deferrals (~24h at 15-min ticks), force.
        self.max_deferrals = _env_int("COSMIC_RESTART_MAX_DEFERRALS", 96)
        self.drain_settle_sec = _env_float("COSMIC_DRAIN_SETTLE_SEC", 6.0)
        self.health_timeout_sec = _env_int("COSMIC_HEALTH_TIMEOUT_SEC", 180)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


def parse_epoch(value: str | float | int | None) -> float | None:
    """Parse an ISO timestamp or epoch number into a unix epoch float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    normalized = text.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# Pure decision logic (unit-tested; no I/O)
# ---------------------------------------------------------------------------

def decide(
    *,
    pending: bool,
    drain_active: bool,
    busy_reasons: list[str],
    deferrals: int,
    max_deferrals: int,
) -> dict[str, object]:
    """Gate decision from a collected snapshot.

    Precedence:
      1. Already draining  -> wait (a restart is literally in progress).
      2. Nothing stale     -> noop.
      3. Cron-imminent is part of busy_reasons and is NEVER overridden -- a
         cron is predictable, so waiting for it is free.
      4. Otherwise busy    -> defer (bounded by max_deferrals).
      5. Idle, or deadline reached with no cron imminent -> restart.
    """
    if drain_active:
        return {"action": "wait", "reason": "drain_flag_present", "busy": list(busy_reasons)}
    if not pending:
        return {"action": "noop", "reason": "nothing_stale", "busy": list(busy_reasons)}

    cron_imminent = "cron_imminent" in busy_reasons
    if cron_imminent:
        return {
            "action": "defer",
            "reason": "cron_imminent",
            "busy": list(busy_reasons),
            "deferrals": deferrals,
        }
    if busy_reasons:
        if deferrals >= max_deferrals:
            return {
                "action": "restart",
                "reason": f"deadline_forced_after_{deferrals}_deferrals",
                "busy": list(busy_reasons),
                "deferrals": deferrals,
            }
        return {
            "action": "defer",
            "reason": "busy:" + ",".join(busy_reasons),
            "busy": list(busy_reasons),
            "deferrals": deferrals,
        }
    if deferrals >= max_deferrals:
        return {
            "action": "restart",
            "reason": f"deadline_forced_after_{deferrals}_deferrals",
            "busy": list(busy_reasons),
            "deferrals": deferrals,
        }
    return {"action": "restart", "reason": "idle_and_stale", "busy": [], "deferrals": deferrals}


# ---------------------------------------------------------------------------
# Collectors (each failure is isolated and reported, never fatal)
# ---------------------------------------------------------------------------

def collect_service_staleness(config: Config) -> dict[str, object]:
    """Any cosmic service whose process predates the last dpkg change is stale."""
    try:
        dpkg_mtime = os.stat(DPKG_STATUS).st_mtime
    except OSError as exc:
        return {"pending": False, "error": f"dpkg_status_unreadable: {exc}"}

    stale: list[str] = []
    unknown: list[str] = []
    for service in config.services:
        try:
            out = subprocess.run(
                [
                    "systemctl",
                    "show",
                    service,
                    "-P",
                    "ActiveEnterTimestamp",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError) as exc:
            unknown.append(f"{service}({exc.__class__.__name__})")
            continue
        started = parse_epoch(out or None)
        if started is None:
            # Inactive/never-started: the restart itself will bring it up.
            stale.append(service)
            continue
        if started < dpkg_mtime - 2.0:  # small skew allowance
            stale.append(service)
    return {"pending": bool(stale or unknown), "stale": stale, "unknown": unknown}


def collect_busy_signals(config: Config) -> tuple[list[str], dict[str, object]]:
    """All busy signals, each named for the decision log. Never raises."""
    now = utcnow().timestamp()
    busy: list[str] = []
    detail: dict[str, object] = {}

    # 1. Recently touched user message: a foreground turn may be in flight.
    messages_db = config.backend_dir / "gateway" / "sessions.db"
    try:
        con = sqlite3.connect(f"file:{messages_db}?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        cur.execute(
            "SELECT created_at FROM messages ORDER BY created_at DESC LIMIT 5"
        )
        recent = any(
            (ts := parse_epoch(row[0])) is not None and now - ts < config.busy_message_sec
            for row in cur.fetchall()
        )
        if recent:
            busy.append("user_turn_recent")
        detail["last_message_check"] = "ok"

        # 2. Active task notebook with recent activity (Alpha builds etc.).
        cur.execute(
            "SELECT updated_at FROM task_notebooks WHERE status='active' ORDER BY updated_at DESC LIMIT 5"
        )
        recent_nb = any(
            (ts := parse_epoch(row[0])) is not None and now - ts < config.busy_notebook_sec
            for row in cur.fetchall()
        )
        if recent_nb:
            busy.append("notebook_active_recent")
        con.close()
    except (sqlite3.Error, OSError) as exc:
        detail["sessions_db"] = f"unavailable: {exc.__class__.__name__}"

    # 3. Orchestrator ledger: non-terminal task touched recently.
    ledger_db = config.backend_dir / "agents" / "orchestrator" / "store" / "data" / "task_ledger.db"
    try:
        con = sqlite3.connect(f"file:{ledger_db}?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        cur.execute(
            "SELECT updated_at FROM tasks WHERE status='running' ORDER BY updated_at DESC LIMIT 5"
        )
        recent_run = any(
            (ts := parse_epoch(row[0])) is not None and now - ts < config.busy_ledger_sec
            for row in cur.fetchall()
        )
        if recent_run:
            busy.append("ledger_task_running")
        con.close()
    except (sqlite3.Error, OSError) as exc:
        detail["task_ledger_db"] = f"unavailable: {exc.__class__.__name__}"

    # 4. Cron about to fire: always respected, even past the deadline.
    scheduler_db = config.backend_dir / "gateway" / "scheduler.db"
    try:
        con = sqlite3.connect(f"file:{scheduler_db}?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        cur.execute(
            "SELECT next_fire_at FROM cron_jobs WHERE paused_at IS NULL AND next_fire_at IS NOT NULL"
        )
        now_iso = iso_now()
        soonest: float | None = None
        for (value,) in cur.fetchall():
            ts = parse_epoch(value)
            if ts is None or ts < now - 86400:  # skip long-overdue/zombie entries
                continue
            if soonest is None or ts < soonest:
                soonest = ts
        if soonest is not None and soonest - now < config.cron_margin_sec:
            busy.append("cron_imminent")
        detail["next_cron_in_sec"] = (
            round(soonest - now) if soonest is not None else None
        )
        con.close()
    except (sqlite3.Error, OSError) as exc:
        detail["scheduler_db"] = f"unavailable: {exc.__class__.__name__}"

    # 5. Outbound deliveries waiting to go out.
    delivery_db = config.backend_dir / "gateway" / "delivery_queue.db"
    try:
        con = sqlite3.connect(f"file:{delivery_db}?mode=ro", uri=True, timeout=5)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM delivery_queue WHERE status='pending'")
        pending_count = int(cur.fetchone()[0])
        if pending_count > 0:
            busy.append("deliveries_pending")
        detail["pending_deliveries"] = pending_count
        con.close()
    except (sqlite3.Error, OSError) as exc:
        detail["delivery_db"] = f"unavailable: {exc.__class__.__name__}"

    # 6. Alpha/opencode child work: long builds are normal; they are what the
    #    deadline path exists for, but an agent tick should never yank one
    #    while it still has plenty of deferral budget.
    try:
        result = subprocess.run(
            ["pgrep", "-f", "opencode"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            busy.append("alpha_build_running")
        detail["opencode_pids"] = len(result.stdout.split())
    except (subprocess.SubprocessError, OSError) as exc:
        detail["pgrep"] = f"unavailable: {exc.__class__.__name__}"

    return busy, detail


def load_state(config: Config) -> dict[str, object]:
    try:
        return json.loads(config.state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"deferrals": 0, "last_restart_at": None, "last_reason": None}


def save_state(config: Config, state: dict[str, object]) -> None:
    config.state_path.parent.mkdir(parents=True, exist_ok=True)
    config.state_path.write_text(
        json.dumps(state, indent=1) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def drain_flag_path(config: Config) -> Path:
    return config.drain_flag


def raise_drain_flag(config: Config) -> None:
    config.drain_flag.parent.mkdir(parents=True, exist_ok=True)
    config.drain_flag.write_text(
        json.dumps({"raised_at": iso_now()}) + "\n", encoding="utf-8"
    )
    os.chmod(config.drain_flag, 0o644)


def clear_drain_flag(config: Config) -> None:
    try:
        config.drain_flag.unlink()
    except FileNotFoundError:
        pass


def wait_for_gateway_health(config: Config) -> bool:
    import urllib.request

    deadline = time.monotonic() + config.health_timeout_sec
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(config.health_url, timeout=5) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(3)
    return False


def graceful_restart(config: Config, *, reason: str) -> dict[str, object]:
    started = iso_now()
    print(f"[cosmic-restart] drain start ({reason}) at {started}", flush=True)
    raise_drain_flag(config)
    time.sleep(config.drain_settle_sec)

    services = list(config.services)
    subprocess.run(["systemctl", "restart", *services], check=False)

    healthy = wait_for_gateway_health(config)
    # Clear the flag only once the gateway can serve again; if it never came
    # up, leave the flag so senders keep retrying while we debug.
    if healthy:
        clear_drain_flag(config)
    result = {
        "restarted_at": started,
        "finished_at": iso_now(),
        "reason": reason,
        "healthy": healthy,
    }
    print(f"[cosmic-restart] done healthy={healthy}", flush=True)
    return result


def run_agent(config: Config) -> int:
    state = load_state(config)
    deferrals = int(state.get("deferrals") or 0)

    staleness = collect_service_staleness(config)
    busy, detail = collect_busy_signals(config)
    decision = decide(
        pending=bool(staleness.get("pending")),
        drain_active=drain_flag_path(config).exists(),
        busy_reasons=busy,
        deferrals=deferrals,
        max_deferrals=config.max_deferrals,
    )
    action = str(decision["action"])
    print(
        json.dumps(
            {
                "decision": decision,
                "staleness": staleness,
                "detail": detail,
                "state": {"deferrals": deferrals},
            },
            default=str,
        ),
        flush=True,
    )

    if action == "restart":
        result = graceful_restart(config, reason=str(decision["reason"]))
        state.update(
            {
                "deferrals": 0,
                "last_restart_at": result["finished_at"],
                "last_reason": decision["reason"],
                "last_restart_healthy": result["healthy"],
            }
        )
        save_state(config, state)
        return 0 if result["healthy"] else 1

    if action == "defer":
        state.update({"deferrals": deferrals + 1, "last_reason": decision["reason"]})
        save_state(config, state)
    elif action == "noop":
        if deferrals:
            state.update({"deferrals": 0, "last_reason": decision["reason"]})
            save_state(config, state)
    return 0


def run_status(config: Config) -> int:
    state = load_state(config)
    staleness = collect_service_staleness(config)
    busy, detail = collect_busy_signals(config)
    decision = decide(
        pending=bool(staleness.get("pending")),
        drain_active=drain_flag_path(config).exists(),
        busy_reasons=busy,
        deferrals=int(state.get("deferrals") or 0),
        max_deferrals=config.max_deferrals,
    )
    print(
        json.dumps(
            {
                "decision": decision,
                "staleness": staleness,
                "detail": detail,
                "state": state,
                "drain_flag": drain_flag_path(config).exists(),
            },
            default=str,
            indent=1,
        )
    )
    return 0


def run_restart(config: Config) -> int:
    result = graceful_restart(config, reason="manual")
    state = load_state(config)
    state.update(
        {
            "deferrals": 0,
            "last_restart_at": result["finished_at"],
            "last_reason": "manual",
            "last_restart_healthy": result["healthy"],
        }
    )
    save_state(config, state)
    return 0 if result["healthy"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", choices=["agent", "restart", "status"], help="see module docstring"
    )
    args = parser.parse_args(argv)
    config = Config()
    if args.command == "agent":
        return run_agent(config)
    if args.command == "restart":
        return run_restart(config)
    return run_status(config)


if __name__ == "__main__":
    raise SystemExit(main())
