"""Tests for the gateway's specialist-completion artifact persistence path.

When the orchestrator's `delegate_to_agent` returns ``in_progress`` (long-running
specialists like the slide agent), the eventual ``task.completed`` event lands
in the gateway specialist event consumer and previously was never registered as
output artifacts — orphaning deliverables on disk. This guards the fix.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.runtime import GatewayRuntime


def _bare_runtime(tmp_path: Path | None = None) -> GatewayRuntime:
    """Construct a GatewayRuntime without running its real __init__.

    `_persist_specialist_completion_artifacts` only needs a handful of helpers,
    not the full Redis/Anthropic/store stack — so we bypass __init__ and stub
    the few attributes its dependencies (mainly `_normalize_produced_artifact_list`
    via `_resolve_logical_artifact_path`) actually touch.
    """
    rt = GatewayRuntime.__new__(GatewayRuntime)
    rt.config = SimpleNamespace(  # type: ignore[attr-defined]
        artifacts_root=tmp_path or Path("/tmp/_cosmic_test_artifacts_root"),
    )
    rt._cache_artifact_list = MagicMock()  # type: ignore[method-assign]
    return rt


def _slide_artifact(audience: str = "deliverable", artifact_id: str = "art_x") -> dict:
    return {
        "artifact_id": artifact_id,
        "task_id": "tsk_specialist_1",
        "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "sha256": "deadbeef",
        "path": "runs/artifacts/tsk_specialist_1/slide_agent/deck.pptx",
        "created_by_agent": "cosmic/slide-agent:1.0.0",
        "created_at": "2026-05-08T19:05:30Z",
        "kind": "output",
        "audience": audience,
    }


def _forwarded(**overrides) -> dict:
    base = {
        "request_id": "req_abc",
        "session_id": "sess_xyz",
        "channel": "desktop:desk_test",
        "task_id": "tsk_root",
    }
    base.update(overrides)
    return base


def test_completion_event_with_artifacts_is_persisted():
    rt = _bare_runtime()
    event = SimpleNamespace(
        event_type="task.completed",
        payload={"status": "completed", "artifacts": [_slide_artifact()]},
    )

    rt._persist_specialist_completion_artifacts(event=event, forwarded=_forwarded())

    rt._cache_artifact_list.assert_called_once()
    kwargs = rt._cache_artifact_list.call_args.kwargs
    assert kwargs["request_id"] == "req_abc"
    assert kwargs["session_id"] == "sess_xyz"
    assert kwargs["source_channel"] == "desktop:desk_test"
    assert kwargs["source_message_id"] is None
    assert isinstance(kwargs["artifacts"], list) and len(kwargs["artifacts"]) == 1
    persisted = kwargs["artifacts"][0]
    assert persisted["artifact_id"] == "art_x"
    assert persisted["mime_type"].endswith("presentationml.presentation")


def test_progress_events_do_not_trigger_persistence():
    rt = _bare_runtime()
    event = SimpleNamespace(
        event_type="task.progress",
        payload={"artifacts": [_slide_artifact()]},
    )
    rt._persist_specialist_completion_artifacts(event=event, forwarded=_forwarded())
    rt._cache_artifact_list.assert_not_called()


def test_failed_events_do_not_trigger_persistence():
    rt = _bare_runtime()
    event = SimpleNamespace(
        event_type="task.failed",
        payload={"artifacts": [_slide_artifact()]},
    )
    rt._persist_specialist_completion_artifacts(event=event, forwarded=_forwarded())
    rt._cache_artifact_list.assert_not_called()


def test_completion_without_artifacts_is_skipped():
    rt = _bare_runtime()
    event = SimpleNamespace(event_type="task.completed", payload={"status": "completed"})
    rt._persist_specialist_completion_artifacts(event=event, forwarded=_forwarded())
    rt._cache_artifact_list.assert_not_called()


def test_missing_request_context_is_skipped():
    rt = _bare_runtime()
    event = SimpleNamespace(
        event_type="task.completed",
        payload={"artifacts": [_slide_artifact()]},
    )
    # forwarded missing request_id → cannot persist
    rt._persist_specialist_completion_artifacts(
        event=event, forwarded=_forwarded(request_id="")
    )
    rt._cache_artifact_list.assert_not_called()


def test_supporting_artifacts_are_persisted_too():
    """Supporting artifacts (plan.json, theme.json) should also be persisted so
    the orchestrator can re-surface them on follow-up turns. Audience filtering
    is the responsibility of consumers, not the gateway store."""
    rt = _bare_runtime()
    event = SimpleNamespace(
        event_type="task.completed",
        payload={
            "artifacts": [
                _slide_artifact(audience="deliverable", artifact_id="art_deck"),
                _slide_artifact(audience="supporting", artifact_id="art_plan"),
            ],
        },
    )
    rt._persist_specialist_completion_artifacts(event=event, forwarded=_forwarded())
    persisted = rt._cache_artifact_list.call_args.kwargs["artifacts"]
    ids = {item["artifact_id"] for item in persisted}
    assert ids == {"art_deck", "art_plan"}


def test_persistence_failure_is_swallowed_and_logged(caplog):
    """A failure in the persistence path must never break the UI event flow."""
    rt = _bare_runtime()
    rt._cache_artifact_list.side_effect = RuntimeError("boom")  # type: ignore[union-attr]
    event = SimpleNamespace(
        event_type="task.completed",
        payload={"artifacts": [_slide_artifact()]},
    )
    # Should NOT raise.
    rt._persist_specialist_completion_artifacts(event=event, forwarded=_forwarded())
    assert any(
        "specialist_completion_artifact_persist_failed" in record.message
        for record in caplog.records
    )
