from __future__ import annotations

import pytest

from gateway.orchestrator_client import OrchestratorClient


@pytest.mark.asyncio
async def test_orchestrator_client_disables_stream_read_timeout() -> None:
    client = OrchestratorClient(
        base_url="http://127.0.0.1:8743",
        internal_token="internal-token",
        timeout_sec=300.0,
    )
    try:
        assert client._stream_timeout.read is None
        assert client._stream_timeout.connect == 10.0
        assert client._client.timeout.read == 300.0
    finally:
        await client.stop()
