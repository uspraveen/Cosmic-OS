"""Tests for the activity-aware restart control (scripts/cosmic_restart_ctl.py)
and the gateway maintenance drain flag (gateway/maintenance.py)."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BACKEND_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import cosmic_restart_ctl as ctl  # noqa: E402

from gateway import maintenance  # noqa: E402


# ---------------------------------------------------------------------------
# decide(): the gate
# ---------------------------------------------------------------------------

def _decide(**overrides):
    base = dict(
        pending=True,
        drain_active=False,
        busy_reasons=[],
        deferrals=0,
        max_deferrals=96,
    )
    base.update(overrides)
    return ctl.decide(**base)


def test_nothing_stale_is_noop():
    decision = _decide(pending=False, busy_reasons=["user_turn_recent"])
    assert decision["action"] == "noop"


def test_idle_and_stale_restarts():
    decision = _decide()
    assert decision["action"] == "restart"
    assert decision["reason"] == "idle_and_stale"


def test_busy_defers_with_reasons():
    decision = _decide(busy_reasons=["user_turn_recent", "ledger_task_running"])
    assert decision["action"] == "defer"
    assert "user_turn_recent" in decision["reason"]


def test_cron_imminent_alone_defers():
    decision = _decide(busy_reasons=["cron_imminent"])
    assert decision["action"] == "defer"
    assert decision["reason"] == "cron_imminent"


def test_cron_imminent_is_respected_even_at_deadline():
    decision = _decide(busy_reasons=["cron_imminent"], deferrals=96, max_deferrals=96)
    assert decision["action"] == "defer"


def test_deadline_forces_past_other_busy_reasons():
    decision = _decide(
        busy_reasons=["alpha_build_running"], deferrals=96, max_deferrals=96
    )
    assert decision["action"] == "restart"
    assert decision["reason"].startswith("deadline_forced")


def test_cron_imminent_wins_over_other_busy_at_deadline():
    decision = _decide(
        busy_reasons=["alpha_build_running", "cron_imminent"],
        deferrals=96,
        max_deferrals=96,
    )
    assert decision["action"] == "defer"
    assert decision["reason"] == "cron_imminent"


def test_drain_flag_waits_over_everything():
    decision = _decide(drain_active=True)
    assert decision["action"] == "wait"


# ---------------------------------------------------------------------------
# parse_epoch()
# ---------------------------------------------------------------------------

def test_parse_epoch_iso_z():
    assert ctl.parse_epoch("2026-09-18T16:45:08.436999Z") is not None


def test_parse_epoch_naive_iso_gets_utc():
    value = ctl.parse_epoch("2026-09-18 16:45:08")
    assert value is not None


def test_parse_epoch_garbage_is_none():
    assert ctl.parse_epoch("not-a-timestamp") is None
    assert ctl.parse_epoch("") is None
    assert ctl.parse_epoch(None) is None


def test_parse_epoch_numeric_string():
    assert ctl.parse_epoch("1758211508.0") == 1758211508.0


def test_parse_epoch_systemd_active_enter_timestamp():
    value = ctl.parse_epoch("Fri 2026-09-18 16:47:34 UTC")
    assert value is not None
    # 2026-09-18T16:47:34Z
    from datetime import datetime, timezone

    assert datetime.fromtimestamp(value, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    ) == "2026-09-18T16:47:34"


def test_parse_epoch_systemd_gmt_variant():
    assert ctl.parse_epoch("Sat 2026-09-19 03:02:10 GMT") is not None


# ---------------------------------------------------------------------------
# maintenance drain flag
# ---------------------------------------------------------------------------

def test_request_should_drain_matches_retried_webhooks_only():
    assert maintenance.request_should_drain("/webhooks/gmail/pubsub")
    assert maintenance.request_should_drain("/webhooks/github")
    assert maintenance.request_should_drain("/internal/channels/agent-email/incoming")
    assert maintenance.request_should_drain("/internal/channels/whatsapp/incoming")
    assert maintenance.request_should_drain("/channels/telegram/webhook")
    # Interactive paths without sender-side retry must never be drained.
    assert not maintenance.request_should_drain("/health")
    assert not maintenance.request_should_drain("/channels/desktop/uploads")
    assert not maintenance.request_should_drain("/scheduler/crons")


def test_drain_retry_response_shape():
    status, body, headers = maintenance.drain_retry_response()
    assert status == 503
    assert headers["Retry-After"] == "60"
    assert "restart" in body


def test_drain_active_follows_flag_file(tmp_path, monkeypatch):
    flag = tmp_path / "drain"
    monkeypatch.setenv("COSMIC_DRAIN_FLAG", str(flag))
    assert maintenance.drain_active() is False
    flag.write_text("{}\n", encoding="utf-8")
    assert maintenance.drain_active() is True
    flag.unlink()
    assert maintenance.drain_active() is False
