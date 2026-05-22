from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.runtime import GatewayRuntime


def _runtime() -> GatewayRuntime:
    runtime = object.__new__(GatewayRuntime)
    runtime.request_records = {
        "req_heartbeat_test": {
            "source": "heartbeat",
        }
    }
    return runtime


def test_heartbeat_ok_with_process_narration_is_suppressed() -> None:
    runtime = _runtime()

    assert runtime._is_heartbeat_noop_response(
        {
            "type": "response.complete",
            "request_id": "req_heartbeat_test",
            "content": "I'll do a quick check for new YC chatter.\nheartbeat_ok",
        }
    )


def test_heartbeat_ok_embedded_in_other_word_is_not_suppressed() -> None:
    runtime = _runtime()

    assert not runtime._is_heartbeat_noop_response(
        {
            "type": "response.complete",
            "request_id": "req_heartbeat_test",
            "content": "heartbeat_okay, here is a real note.",
        }
    )
