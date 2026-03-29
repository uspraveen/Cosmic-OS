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
