from pathlib import Path

from gateway.request_trace_store import RequestTraceStore


def test_request_trace_store_records_and_lists_session_traces(tmp_path: Path) -> None:
    store = RequestTraceStore(tmp_path / "request_traces.db")
    store.initialize()

    store.record_event(
        request_id="req_123",
        session_id="sess_123",
        channel="desktop:desk_1",
        route="opus",
        event_type="request.accepted",
        stage="accepted",
        status="active",
        title="Request accepted",
        detail="Generate something interesting.",
        user_query_excerpt="Generate something interesting.",
    )
    store.record_event(
        request_id="req_123",
        session_id="sess_123",
        channel="desktop:desk_1",
        route="opus",
        event_type="response.complete",
        stage="response",
        status="completed",
        title="Assistant response completed",
        detail="Done.",
        task_id="tsk_123",
        specialist_receipts=[{"intent": "image.generate", "provider": "openai", "model": "gpt-image-1.5"}],
        delivery={"status": "sent"},
        completed=True,
    )

    traces = store.list_session_traces("sess_123")

    assert len(traces) == 1
    trace = traces[0]
    assert trace["request_id"] == "req_123"
    assert trace["task_id"] == "tsk_123"
    assert trace["status"] == "completed"
    assert trace["final_event_type"] == "response.complete"
    assert trace["delivery"] == {"status": "sent"}
    assert trace["specialist_receipts"] == [{"intent": "image.generate", "provider": "openai", "model": "gpt-image-1.5"}]
    assert len(trace["events"]) == 2
    assert trace["events"][0]["event_type"] == "request.accepted"
    assert trace["events"][1]["event_type"] == "response.complete"


def test_request_trace_store_does_not_let_later_housekeeping_event_downgrade_sent_status(
    tmp_path: Path,
) -> None:
    """Reproduces a real production bug: an agent-email reply is delivered
    successfully (status="sent" on the response.complete event), then the
    stream's trailing task.completed housekeeping event - which correctly has
    no new content to send, so it records status="skipped" - must not
    overwrite the already-correct "sent" summary. The full event log should
    still capture both events exactly as they happened."""
    store = RequestTraceStore(tmp_path / "request_traces.db")
    store.initialize()

    store.record_event(
        request_id="req_email_1",
        session_id="sess_email_1",
        channel="agent-email:iamcosmic001@mail.example.com",
        route="opus",
        event_type="delivery.response.complete",
        stage="delivery",
        status="sent",
        title="Channel delivery completed",
        detail="Gateway delivery=sent; email delivery=sent",
        task_id="tsk_email_1",
        delivery={"status": "sent", "message_id": "msg_abc"},
        completed=True,
    )
    store.record_event(
        request_id="req_email_1",
        session_id="sess_email_1",
        channel="agent-email:iamcosmic001@mail.example.com",
        route="opus",
        event_type="delivery.task.completed",
        stage="delivery",
        status="skipped",
        title="Channel delivery completed",
        detail="Gateway delivery=sent; email delivery=skipped",
        task_id="tsk_email_1",
        delivery={"status": "skipped", "reason": "non_sendable_event"},
        completed=False,
    )

    trace = store.get_request_trace("req_email_1")

    assert trace is not None
    assert trace["status"] == "sent"
    assert trace["delivery"] == {"status": "sent", "message_id": "msg_abc"}
    assert trace["final_event_type"] == "delivery.response.complete"
    assert len(trace["events"]) == 2
    assert trace["events"][0]["event_type"] == "delivery.response.complete"
    assert trace["events"][0]["status"] == "sent"
    assert trace["events"][1]["event_type"] == "delivery.task.completed"
    assert trace["events"][1]["status"] == "skipped"


def test_request_trace_store_get_request_trace_returns_single_record(tmp_path: Path) -> None:
    store = RequestTraceStore(tmp_path / "request_traces.db")
    store.initialize()

    store.record_event(
        request_id="req_single",
        session_id="sess_single",
        channel="agent-email:assistant@example.com",
        route="opus",
        event_type="email.process_inbound.completed",
        stage="email_preprocess",
        status="completed",
        title="Email preprocess completed",
        detail="Trusted sender matched.",
    )

    trace = store.get_request_trace("req_single")

    assert trace is not None
    assert trace["request_id"] == "req_single"
    assert trace["session_id"] == "sess_single"
    assert trace["events"][0]["stage"] == "email_preprocess"
