import base64
import json
import httpx
import pytest
import shutil
import tempfile
from pathlib import Path

from gateway.config import GatewayConfig
from gateway.runtime import GatewayRuntime
from orchestrator.config import OrchestratorConfig
from orchestrator.visual_enrichment import VisualEnrichmentCoordinator


def _make_test_dir(prefix: str) -> Path:
    root = Path.cwd() / ".codex_test_tmp"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def _tiny_png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2u0AAAAASUVORK5CYII="
    )


def _build_low_confidence_image_transport() -> httpx.MockTransport:
    image_bytes = _tiny_png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v2/scrape"):
            payload = json.loads(request.content.decode("utf-8"))
            url = payload.get("url")
            if url == "https://example.com/source-1":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"images": [], "metadata": {"title": "Source one"}}},
                )
            if url == "https://example.com/source-2":
                return httpx.Response(
                    200,
                    json={"success": True, "data": {"images": [], "metadata": {"title": "Source two"}}},
                )
            if url == "https://example.com/source-3":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "images": ["https://cdn.example.test/source-3-hero.png"],
                            "metadata": {"title": "Source three"},
                        },
                    },
                )
            raise AssertionError(f"unexpected scrape url {url!r}")
        if request.method == "GET" and str(request.url) == "https://cdn.example.test/source-3-hero.png":
            return httpx.Response(
                200,
                content=image_bytes,
                headers={"Content-Type": "image/png"},
            )
        raise AssertionError(f"unexpected request {request.method} {request.url!s}")

    return httpx.MockTransport(handler)


def _note_three_generic_sources(coordinator: VisualEnrichmentCoordinator) -> None:
    coordinator.note_sources(
        [
            {"url": "https://example.com/source-1", "title": "Source one", "domain": "example.com"},
            {"url": "https://example.com/source-2", "title": "Source two", "domain": "example.com"},
            {"url": "https://example.com/source-3", "title": "Source three", "domain": "example.com"},
        ]
    )


def _build_gateway_runtime(tmp_path: Path) -> GatewayRuntime:
    runtime = GatewayRuntime(
        GatewayConfig(
            local_api_token="test-token",
            internal_token="internal-token",
            signing_secret="signing-secret",
            model_router_url="http://127.0.0.1:9999",
            orchestrator_url="http://127.0.0.1:8743",
            enable_whatsapp=False,
            preferences_db_path=tmp_path / "preferences.db",
            sessions_db_path=tmp_path / "sessions.db",
            routing_audit_db_path=tmp_path / "routing_audit.db",
            artifacts_db_path=tmp_path / "artifacts.db",
            delivery_queue_db_path=tmp_path / "delivery_queue.db",
            scheduler_db_path=tmp_path / "scheduler.db",
            memory_write_audit_db_path=tmp_path / "memory_write_audit.db",
        )
    )
    runtime.session_store.initialize()
    runtime.preference_store.initialize()
    runtime.request_trace_store.initialize()
    runtime.routing_audit_store.initialize()
    runtime.artifact_store.initialize()
    return runtime


def test_runtime_builds_client_response_blocks_with_supporting_image_preview(
) -> None:
    root = _make_test_dir("visual-block-preview-")
    try:
        runtime = _build_gateway_runtime(root)
        runtime.config.artifacts_root = root / "runs" / "artifacts"
        runtime.config.public_base_url = "https://gateway.example.test"

        artifact_dir = runtime.config.artifacts_root / "unit"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / "inline-chart.png"
        artifact_path.write_bytes(b"\x89PNG\r\n\x1a\ninline-chart")

        supporting_artifacts = runtime._normalize_produced_artifact_list(
            [
                {
                    "artifact_id": "art_inline_chart",
                    "path": "runs/artifacts/unit/inline-chart.png",
                    "filename": "inline-chart.png",
                    "mime_type": "image/png",
                    "downloadable": False,
                    "caption": "Quarterly revenue trend",
                }
            ]
        )

        blocks = runtime._build_client_response_blocks(
            content=None,
            supporting_artifacts=supporting_artifacts,
            stored_blocks=[
                {
                    "id": "chart_1",
                    "type": "image_artifact",
                    "artifact_id": "art_inline_chart",
                    "filename": "inline-chart.png",
                    "caption": "Quarterly revenue trend",
                }
            ],
        )

        assert len(blocks) == 1
        assert blocks[0]["type"] == "image_artifact"
        assert blocks[0]["artifact_id"] == "art_inline_chart"
        assert blocks[0]["preview_url"].startswith(
            "https://gateway.example.test/artifacts/content/art_inline_chart?"
        )
        assert blocks[0]["downloadable"] is False
        assert blocks[0]["caption"] == "Quarterly revenue trend"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_response_blocks_snapshot_hydrates_supporting_artifacts_for_delivery(
) -> None:
    root = _make_test_dir("visual-snapshot-")
    try:
        runtime = _build_gateway_runtime(root)
        runtime.config.artifacts_root = root / "runs" / "artifacts"
        runtime.config.public_base_url = "https://gateway.example.test"

        artifact_dir = runtime.config.artifacts_root / "unit"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / "inline-image.png"
        artifact_path.write_bytes(b"\x89PNG\r\n\x1a\ninline-image")

        sent_events: list[dict] = []

        async def send(event: dict) -> None:
            sent_events.append(event)

        def store_assistant_message(
            content: str,
            *,
            awaiting_reply: bool,
            metadata: dict | None,
            channel: str | None,
            route: str | None,
        ) -> str:
            raise AssertionError("response.blocks.snapshot should not persist an assistant message")

        await runtime._handle_orchestrator_event(
            {
                "type": "response.blocks.snapshot",
                "request_id": "req_visual_snapshot_1",
                "task_id": "tsk_visual_snapshot_1",
                "session_id": "sess_visual_snapshot_1",
                "channel": "desktop:desk_visual_snapshot_1",
                "snapshot_seq": 2,
                "response_blocks": [
                    {
                        "id": "markdown_1",
                        "type": "markdown",
                        "text": "Here is the chart.\n\n",
                    },
                    {
                        "id": "chart_1",
                        "type": "image_artifact",
                        "artifact_id": "art_inline_image",
                        "filename": "inline-image.png",
                        "caption": "Inline chart preview",
                    },
                ],
                "supporting_artifacts": [
                    {
                        "artifact_id": "art_inline_image",
                        "path": "runs/artifacts/unit/inline-image.png",
                        "filename": "inline-image.png",
                        "mime_type": "image/png",
                        "downloadable": False,
                    }
                ],
            },
            send=send,
            store_assistant_message=store_assistant_message,
        )

        assert len(sent_events) == 1
        snapshot = sent_events[0]
        assert snapshot["type"] == "response.blocks.snapshot"
        assert snapshot["snapshot_seq"] == 2
        assert snapshot["response_blocks"][1]["type"] == "image_artifact"
        assert snapshot["response_blocks"][1]["preview_url"].startswith(
            "https://gateway.example.test/artifacts/content/art_inline_image?"
        )
        assert snapshot["response_blocks"][1]["downloadable"] is False

        cached = runtime.artifact_store.get("art_inline_image")
        assert cached is not None
        assert cached["filename"] == "inline-image.png"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_coordinator_generates_inline_chart_supporting_artifact(
) -> None:
    root = _make_test_dir("visual-chart-")
    try:
        config = OrchestratorConfig(
            artifacts_root=root / "artifacts",
            internal_token="internal-token",
            signing_secret="signing-secret",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-opus-4-6",
            task_ledger_db_path=root / "task_ledger.db",
            visual_enhancement_enabled=True,
            visual_finalization_grace_ms=1200,
            visual_max_concurrent_sidecars=1,
            visual_max_chart_slots_per_turn=1,
        )
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_chart_1",
                request_id="req_visual_chart_1",
                session_id="sess_visual_chart_1",
                channel="desktop:desk_visual_chart_1",
                user_query="Compare quarterly revenue growth.",
                http_client=client,
            )

            visible_delta, snapshot_events = coordinator.consume_text(
                (
                    "Revenue increased across the year.\n\n"
                    "[[visual_slot {\"id\":\"chart_1\",\"kind\":\"chart\",\"caption\":\"Quarterly revenue trend\","
                    "\"chart_type\":\"line\",\"title\":\"Quarterly revenue\",\"x_label\":\"Quarter\","
                    "\"y_label\":\"Revenue\",\"series\":[{\"label\":\"Revenue\",\"points\":["
                    "{\"x\":\"Q1\",\"y\":10},{\"x\":\"Q2\",\"y\":14},{\"x\":\"Q3\",\"y\":18},{\"x\":\"Q4\",\"y\":23}"
                    "]}]}]]\n\n"
                    "The strongest acceleration happened in the second half."
                )
            )

            assert "[[visual_slot" not in visible_delta
            assert snapshot_events
            assert snapshot_events[0]["type"] == "response.blocks.snapshot"
            assert any(
                block.get("type") == "chart_slot"
                for block in snapshot_events[0]["response_blocks"]
            )

            final_payload = await coordinator.finalize()

        final_blocks = final_payload["response_blocks"]
        block_types = [block["type"] for block in final_blocks]
        assert block_types.count("image_artifact") == 1
        chart_block = next(
            block for block in final_blocks if block["type"] == "image_artifact"
        )
        assert chart_block["id"] == "chart_1"
        assert chart_block["kind"] == "chart"
        assert chart_block["caption"] == "Quarterly revenue trend"
        assert chart_block["provenance"]["attribution_label"] == "Generated chart"
        assert "Revenue increased across the year." in final_payload["content"]
        assert (
            "The strongest acceleration happened in the second half."
            in final_payload["content"]
        )
        assert len(final_payload["supporting_artifacts"]) == 1
        assert final_payload["supporting_artifacts"][0]["downloadable"] is False
        assert Path(final_payload["supporting_artifacts"][0]["path"]).exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_explicit_image_request_uses_relaxed_trusted_fallback(
) -> None:
    root = _make_test_dir("visual-image-explicit-")
    try:
        config = OrchestratorConfig(
            artifacts_root=root / "artifacts",
            internal_token="internal-token",
            signing_secret="signing-secret",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-opus-4-6",
            task_ledger_db_path=root / "task_ledger.db",
            visual_enhancement_enabled=True,
            visual_finalization_grace_ms=1200,
            visual_max_concurrent_sidecars=1,
            visual_max_image_slots_per_turn=1,
            visual_image_min_confidence=0.58,
            visual_firecrawl_api_key="firecrawl-key",
        )
        transport = _build_low_confidence_image_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_explicit_1",
                request_id="req_visual_image_explicit_1",
                session_id="sess_visual_image_explicit_1",
                channel="desktop:desk_visual_image_explicit_1",
                user_query="Tell me more about this. Use your inline image feature to show some relevant images.",
                http_client=client,
            )
            _note_three_generic_sources(coordinator)

            _, snapshot_events = coordinator.consume_text(
                (
                    "Here is more context about the deal.\n\n"
                    "[[visual_slot {\"id\":\"img_1\",\"kind\":\"image\",\"query\":\"deal coverage visual\","
                    "\"caption\":\"Relevant article image\"}]]\n\n"
                    "The funding pressure matters because it changed the negotiating leverage."
                )
            )

            assert snapshot_events
            assert any(
                block.get("type") == "image_slot"
                for block in snapshot_events[0]["response_blocks"]
            )

            final_payload = await coordinator.finalize()

        final_blocks = final_payload["response_blocks"]
        image_block = next(
            block for block in final_blocks if block["type"] == "image_artifact"
        )
        assert image_block["id"] == "img_1"
        assert "explicitly requested inline imagery" in image_block["provenance"]["selection_reason"]
        assert len(final_payload["supporting_artifacts"]) == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_non_explicit_image_request_keeps_strict_threshold(
) -> None:
    root = _make_test_dir("visual-image-strict-")
    try:
        config = OrchestratorConfig(
            artifacts_root=root / "artifacts",
            internal_token="internal-token",
            signing_secret="signing-secret",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-opus-4-6",
            task_ledger_db_path=root / "task_ledger.db",
            visual_enhancement_enabled=True,
            visual_finalization_grace_ms=1200,
            visual_max_concurrent_sidecars=1,
            visual_max_image_slots_per_turn=1,
            visual_image_min_confidence=0.58,
            visual_firecrawl_api_key="firecrawl-key",
        )
        transport = _build_low_confidence_image_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_strict_1",
                request_id="req_visual_image_strict_1",
                session_id="sess_visual_image_strict_1",
                channel="desktop:desk_visual_image_strict_1",
                user_query="Tell me more about this deal.",
                http_client=client,
            )
            _note_three_generic_sources(coordinator)

            coordinator.consume_text(
                (
                    "Here is more context about the deal.\n\n"
                    "[[visual_slot {\"id\":\"img_1\",\"kind\":\"image\",\"query\":\"deal coverage visual\","
                    "\"caption\":\"Relevant article image\"}]]\n\n"
                    "The funding pressure matters because it changed the negotiating leverage."
                )
            )

            final_payload = await coordinator.finalize()

        assert not any(
            block["type"] == "image_artifact" for block in final_payload["response_blocks"]
        )
        assert final_payload["supporting_artifacts"] == []
    finally:
        shutil.rmtree(root, ignore_errors=True)
