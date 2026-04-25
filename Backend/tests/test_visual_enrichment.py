import base64
import json
import httpx
import pytest
import shutil
import time
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from gateway.config import GatewayConfig
from gateway.runtime import GatewayRuntime
from orchestrator.config import OrchestratorConfig
from orchestrator.visual_enrichment.charting import normalize_chart_spec, render_chart_png
from orchestrator.visual_enrichment.clients import (
    DirectImageSearchClient,
    DirectImageSearchConfig,
)
from orchestrator.visual_enrichment.coordinator import _is_probably_text_art
from orchestrator.visual_enrichment import VisualEnrichmentCoordinator
from PIL import Image


def _make_test_dir(prefix: str) -> Path:
    root = Path.cwd() / ".codex_test_tmp"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{prefix}{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tiny_png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2u0AAAAASUVORK5CYII="
    )


def _solid_png_bytes(width: int, height: int) -> bytes:
    image = Image.new("RGBA", (width, height), (12, 18, 28, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_low_confidence_image_transport() -> httpx.MockTransport:
    image_bytes = _solid_png_bytes(1280, 720)

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
                            "images": [
                                {
                                    "src": "https://cdn.example.test/source-3-hero.png",
                                    "alt": "Deal",
                                }
                            ],
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


def _build_next_image_proxy_transport() -> httpx.MockTransport:
    image_bytes = _solid_png_bytes(1280, 720)
    normalized_asset_url = "https://x.ai/_next/static/media/colossus-racks.8af37456.webp"
    proxied_image_url = (
        "https://x.ai/_next/image?url=%2F_next%2Fstatic%2Fmedia%2Fcolossus-racks.8af37456.webp"
        "&w=3840&q=75"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v2/scrape"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("url") != "https://example.com/colossus":
                raise AssertionError(f"unexpected scrape url {payload.get('url')!r}")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "images": [
                            {
                                "src": proxied_image_url,
                                "alt": "Colossus supercomputer GPU racks in Memphis",
                                "width": 3840,
                                "height": 2160,
                            }
                        ],
                        "metadata": {"title": "Inside Colossus"},
                    },
                },
            )
        if request.method == "GET" and str(request.url) == normalized_asset_url:
            return httpx.Response(
                200,
                content=image_bytes,
                headers={"Content-Type": "image/png"},
            )
        if request.method == "GET" and str(request.url) == proxied_image_url:
            raise AssertionError("proxy image URL should have been normalized before download")
        raise AssertionError(f"unexpected request {request.method} {request.url!s}")

    return httpx.MockTransport(handler)


def _build_retrying_image_transport() -> httpx.MockTransport:
    image_bytes = _solid_png_bytes(1280, 720)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v2/scrape"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("url") != "https://example.com/colossus":
                raise AssertionError(f"unexpected scrape url {payload.get('url')!r}")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "images": [
                            {
                                "src": "https://cdn.example.test/blocked-colossus.png",
                                "alt": "Elon xAI Colossus supercomputer",
                                "width": 1600,
                                "height": 900,
                            },
                            {
                                "src": "https://cdn.example.test/usable-colossus.png",
                                "alt": "xAI Colossus GPU training cluster",
                                "width": 1600,
                                "height": 900,
                            },
                        ],
                        "metadata": {"title": "Colossus overview"},
                    },
                },
            )
        if request.method == "GET" and str(request.url) == "https://cdn.example.test/blocked-colossus.png":
            return httpx.Response(403, content=b"blocked")
        if request.method == "GET" and str(request.url) == "https://cdn.example.test/usable-colossus.png":
            return httpx.Response(
                200,
                content=image_bytes,
                headers={"Content-Type": "image/png"},
            )
        raise AssertionError(f"unexpected request {request.method} {request.url!s}")

    return httpx.MockTransport(handler)


def _build_tiny_image_retry_transport() -> httpx.MockTransport:
    tiny_bytes = _solid_png_bytes(52, 52)
    large_bytes = _solid_png_bytes(1280, 720)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v2/scrape"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("url") != "https://example.com/colossus":
                raise AssertionError(f"unexpected scrape url {payload.get('url')!r}")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "images": [
                            {
                                "src": "https://cdn.example.test/cursor-interface-icon.png",
                                "alt": "Cursor AI coding interface",
                                "width": 52,
                                "height": 52,
                            },
                            {
                                "src": "https://cdn.example.test/colossus-racks.png",
                                "alt": "Colossus GPU racks used for frontier model training",
                                "width": 1280,
                                "height": 720,
                            },
                        ],
                        "metadata": {"title": "Cursor and Colossus overview"},
                    },
                },
            )
        if request.method == "GET" and str(request.url) == "https://cdn.example.test/cursor-interface-icon.png":
            return httpx.Response(
                200,
                content=tiny_bytes,
                headers={"Content-Type": "image/png"},
            )
        if request.method == "GET" and str(request.url) == "https://cdn.example.test/colossus-racks.png":
            return httpx.Response(
                200,
                content=large_bytes,
                headers={"Content-Type": "image/png"},
            )
        raise AssertionError(f"unexpected request {request.method} {request.url!s}")

    return httpx.MockTransport(handler)


def _build_delayed_image_transport(delay_sec: float = 0.45) -> httpx.MockTransport:
    image_bytes = _solid_png_bytes(1280, 720)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v2/scrape"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("url") != "https://example.com/colossus":
                raise AssertionError(f"unexpected scrape url {payload.get('url')!r}")
            time.sleep(delay_sec)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "images": [
                            {
                                "src": "https://cdn.example.test/colossus-facility.png",
                                "alt": "Inside the Colossus GPU facility in Memphis",
                                "width": 1280,
                                "height": 720,
                            }
                        ],
                        "metadata": {"title": "Colossus facility"},
                    },
                },
            )
        if request.method == "GET" and str(request.url) == "https://cdn.example.test/colossus-facility.png":
            return httpx.Response(
                200,
                content=image_bytes,
                headers={"Content-Type": "image/png"},
            )
        raise AssertionError(f"unexpected request {request.method} {request.url!s}")

    return httpx.MockTransport(handler)


def _build_svg_noise_transport() -> httpx.MockTransport:
    image_bytes = _solid_png_bytes(1280, 720)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v2/scrape"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("url") != "https://example.com/strategy":
                raise AssertionError(f"unexpected scrape url {payload.get('url')!r}")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "images": [
                            {
                                "src": "https://www.klover.ai/wp-content/plugins/wpforms-lite/assets/images/submit-spin.svg",
                                "alt": "submit spinner",
                                "width": 160,
                                "height": 160,
                            },
                            {
                                "src": "https://cdn.example.test/colossus-strategy.png",
                                "alt": "Executives planning around Colossus and developer tooling",
                                "width": 1280,
                                "height": 720,
                            },
                        ],
                        "metadata": {"title": "Strategy and AI infrastructure"},
                    },
                },
            )
        if request.method == "GET" and str(request.url) == "https://cdn.example.test/colossus-strategy.png":
            return httpx.Response(
                200,
                content=image_bytes,
                headers={"Content-Type": "image/png"},
            )
        if "submit-spin.svg" in str(request.url):
            raise AssertionError("decorative SVG candidate should have been filtered before download")
        raise AssertionError(f"unexpected request {request.method} {request.url!s}")

    return httpx.MockTransport(handler)


def _build_cross_promo_with_search_transport() -> httpx.MockTransport:
    promo_bytes = _solid_png_bytes(960, 540)
    keyart_bytes = _solid_png_bytes(1600, 900)
    promo_url = (
        "https://staticctf.ubisoft.com/J3yJr34U2pZ2Ieem48Dwy9uqj5PNUQTn/6JtJ2ErqVsdI0j4S8GhhRN/"
        "e072686be0016a374562c3d7d9c998c2/POP_35_ANNIVERSARY_All_Pixel_Princes_960.jpg"
    )
    keyart_url = (
        "https://staticctf.ubisoft.com/J3yJr34U2pZ2Ieem48Dwy9uqj5PNUQTn/6JtJ2ErqVsdI0j4S8GhhRN/"
        "e072686be0016a374562c3d7d9c998c2/ACBFR_KeyArt_StandardEdition_960x540_23.04.26_615PMCEST.jpg"
    )
    bing_html = f"""
    <html><body>
      <a class="iusc" m='{{&quot;murl&quot;:&quot;{keyart_url}&quot;,&quot;purl&quot;:&quot;https://news.ubisoft.com/en-us/article/black-flag-resynced&quot;,&quot;t&quot;:&quot;Assassin&#39;s Creed Black Flag Resynced key art&quot;,&quot;desc&quot;:&quot;Official reveal artwork for Assassin&#39;s Creed Black Flag Resynced&quot;,&quot;imgw&quot;:1600,&quot;imgh&quot;:900}}'></a>
      <a class="iusc" m='{{&quot;murl&quot;:&quot;https://cdn.example.test/unrelated-pirate-collage.jpg&quot;,&quot;purl&quot;:&quot;https://example.com/unrelated&quot;,&quot;t&quot;:&quot;Pirate collage&quot;,&quot;desc&quot;:&quot;Generic pirate art&quot;,&quot;imgw&quot;:1280,&quot;imgh&quot;:720}}'></a>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v2/scrape"):
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("url") != "https://news.ubisoft.com/en-us/article/black-flag-resynced":
                raise AssertionError(f"unexpected scrape url {payload.get('url')!r}")
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "images": [
                            {
                                "src": promo_url,
                                "alt": "Ubisoft anniversary promo art",
                                "title": "Ubisoft anniversary lineup",
                                "width": 960,
                                "height": 540,
                            }
                        ],
                        "metadata": {
                            "title": "Assassin's Creed Black Flag Resynced out July 9: everything you need to know"
                        },
                    },
                },
            )
        if request.method == "GET" and str(request.url).startswith("https://www.bing.com/images/search"):
            return httpx.Response(200, text=bing_html, headers={"Content-Type": "text/html"})
        if request.method == "GET" and str(request.url) == promo_url:
            return httpx.Response(
                200,
                content=promo_bytes,
                headers={"Content-Type": "image/jpeg"},
            )
        if request.method == "GET" and str(request.url) == keyart_url:
            return httpx.Response(
                200,
                content=keyart_bytes,
                headers={"Content-Type": "image/jpeg"},
            )
        if request.method == "GET" and str(request.url) == "https://cdn.example.test/unrelated-pirate-collage.jpg":
            return httpx.Response(
                200,
                content=_solid_png_bytes(1280, 720),
                headers={"Content-Type": "image/jpeg"},
            )
        raise AssertionError(f"unexpected request {request.method} {request.url!s}")

    return httpx.MockTransport(handler)


def _build_source_title_query_transport() -> httpx.MockTransport:
    image_bytes = _solid_png_bytes(1600, 900)
    useful_image_url = "https://cdn.example.test/vijay-bus-complaint.jpg"
    title_led_bing_html = f"""
    <html><body>
      <a class="iusc" m='{{&quot;murl&quot;:&quot;{useful_image_url}&quot;,&quot;purl&quot;:&quot;https://example.com/vijay-bus-story&quot;,&quot;t&quot;:&quot;Vijay flags voters stranded at bus terminals in Tamil Nadu&quot;,&quot;desc&quot;:&quot;Coverage of Vijay&#39;s bus-terminal complaint during the Tamil Nadu election&quot;,&quot;imgw&quot;:1600,&quot;imgh&quot;:900}}'></a>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/images/search":
            query = request.url.params.get("q") or ""
            if "what is this bus complaint" in query.lower():
                return httpx.Response(200, text="<html><body></body></html>")
            if "vijay urges eci to extend voting hours amid transport chaos" in query.lower():
                return httpx.Response(200, text=title_led_bing_html)
            return httpx.Response(200, text="<html><body></body></html>")
        if request.method == "GET" and str(request.url) == useful_image_url:
            return httpx.Response(
                200,
                content=image_bytes,
                headers={"Content-Type": "image/png"},
            )
        raise AssertionError(f"unexpected request {request.method} {request.url!s}")

    return httpx.MockTransport(handler)


def _build_auto_slot_query_transport() -> httpx.MockTransport:
    image_bytes = _solid_png_bytes(1600, 900)
    useful_image_url = "https://cdn.example.test/xai-macrohard-campus.jpg"
    bing_html = f"""
    <html><body>
      <a class="iusc" m='{{&quot;murl&quot;:&quot;{useful_image_url}&quot;,&quot;purl&quot;:&quot;https://x.ai/memphis&quot;,&quot;t&quot;:&quot;xAI Colossus 2 data center campus&quot;,&quot;desc&quot;:&quot;Exterior view of xAI&#39;s Colossus 2 data center campus in the Memphis area&quot;,&quot;imgw&quot;:1600,&quot;imgh&quot;:900}}'></a>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/images/search":
            query = (request.url.params.get("q") or "").lower()
            if "xai colossus 2 data center campus" in query:
                return httpx.Response(200, text=bing_html, headers={"Content-Type": "text/html"})
            return httpx.Response(200, text="<html><body></body></html>")
        if request.method == "GET" and str(request.url) == useful_image_url:
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


def _note_colossus_source(coordinator: VisualEnrichmentCoordinator) -> None:
    coordinator.note_sources(
        [
            {
                "url": "https://example.com/colossus",
                "title": "Colossus overview",
                "domain": "example.com",
            }
        ]
    )


def _note_strategy_source(coordinator: VisualEnrichmentCoordinator) -> None:
    coordinator.note_sources(
        [
            {
                "url": "https://example.com/strategy",
                "title": "Strategy and AI infrastructure",
                "domain": "example.com",
            }
        ]
    )


def _note_black_flag_source(coordinator: VisualEnrichmentCoordinator) -> None:
    coordinator.note_sources(
        [
            {
                "url": "https://news.ubisoft.com/en-us/article/black-flag-resynced",
                "title": "Assassin's Creed Black Flag Resynced out July 9: everything you need to know",
                "domain": "news.ubisoft.com",
            }
        ]
    )


def _note_bus_complaint_source(coordinator: VisualEnrichmentCoordinator) -> None:
    coordinator.note_sources(
        [
            {
                "url": "https://timesofindia.indiatimes.com/city/chennai/tamil-nadu-elections-tvk-vijay-urges-eci-to-extend-voting-hours-amid-transport-chaos/articleshow/130456936.cms",
                "title": "Tamil Nadu elections: TVK chief Vijay urges ECI to extend voting hours amid transport chaos",
                "domain": "timesofindia.indiatimes.com",
            }
        ]
    )


def _note_macrohard_source(coordinator: VisualEnrichmentCoordinator) -> None:
    coordinator.note_sources(
        [
            {
                "url": "https://x.ai/memphis",
                "title": "xAI Colossus 2 Data Center Campus",
                "domain": "x.ai",
            }
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
            visual_image_min_confidence=0.60,
            visual_firecrawl_api_key="firecrawl-key",
            visual_image_search_enabled=False,
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


def test_visual_enrichment_final_blocks_drop_failed_auto_image_slot_when_visual_artifacts_exist(
) -> None:
    blocks = [
        {"id": "markdown_1", "type": "markdown", "text": "Here is the analysis."},
        {
            "id": "img_auto_abc123",
            "type": "image_slot",
            "status": "failed",
            "loading_label": "This inline image took too long to finish.",
        },
        {
            "id": "artifact_1",
            "type": "image_artifact",
            "artifact_id": "anthropic_file_1",
            "filename": "chart.png",
            "mime_type": "image/png",
        },
    ]

    cleaned = VisualEnrichmentCoordinator._clean_final_visual_blocks(blocks)

    assert [block["id"] for block in cleaned] == ["markdown_1", "artifact_1"]


def test_visual_enrichment_final_blocks_keep_failed_explicit_image_slot_without_artifact(
) -> None:
    blocks = [
        {"id": "markdown_1", "type": "markdown", "text": "Here is the analysis."},
        {
            "id": "img_1",
            "type": "image_slot",
            "status": "failed",
            "loading_label": "No reliable inline image was found.",
        },
    ]

    cleaned = VisualEnrichmentCoordinator._clean_final_visual_blocks(blocks)

    assert cleaned == blocks


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
            visual_image_min_confidence=0.60,
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
        failed_slot = next(
            block for block in final_payload["response_blocks"] if block["type"] == "image_slot"
        )
        assert failed_slot["status"] == "failed"
        assert "reliable inline image" in failed_slot["loading_label"].lower()
        assert final_payload["supporting_artifacts"] == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_normalizes_next_image_proxy_urls() -> None:
    root = _make_test_dir("visual-image-proxy-")
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
            visual_firecrawl_api_key="firecrawl-key",
            visual_image_search_enabled=False,
        )
        transport = _build_next_image_proxy_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_proxy_1",
                request_id="req_visual_image_proxy_1",
                session_id="sess_visual_image_proxy_1",
                channel="desktop:desk_visual_image_proxy_1",
                user_query="Is Elon's Colossus such a big deal? Include inline images.",
                http_client=client,
            )
            _note_colossus_source(coordinator)

            coordinator.consume_text(
                (
                    "Colossus is a major infrastructure milestone.\n\n"
                    "[[visual_slot {\"id\":\"img_1\",\"kind\":\"image\",\"query\":\"Elon xAI Colossus supercomputer image\","
                    "\"caption\":\"Colossus infrastructure\"}]]\n\n"
                    "The scale matters because it compresses training timelines."
                )
            )

            final_payload = await coordinator.finalize()

        image_block = next(
            block for block in final_payload["response_blocks"] if block["type"] == "image_artifact"
        )
        assert image_block["provenance"]["source_image_url"] == (
            "https://x.ai/_next/static/media/colossus-racks.8af37456.webp"
        )
        assert len(final_payload["supporting_artifacts"]) == 1
        assert final_payload["supporting_artifacts"][0]["source_image_url"] == (
            "https://x.ai/_next/static/media/colossus-racks.8af37456.webp"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_retries_next_candidate_after_download_failure() -> None:
    root = _make_test_dir("visual-image-retry-")
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
            visual_image_min_confidence=0.45,
            visual_firecrawl_api_key="firecrawl-key",
            visual_image_search_enabled=False,
        )
        transport = _build_retrying_image_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_retry_1",
                request_id="req_visual_image_retry_1",
                session_id="sess_visual_image_retry_1",
                channel="desktop:desk_visual_image_retry_1",
                user_query="Is Elon's Colossus such a big deal? Include inline images.",
                http_client=client,
            )
            _note_colossus_source(coordinator)

            coordinator.consume_text(
                (
                    "Colossus is a major infrastructure milestone.\n\n"
                    "[[visual_slot {\"id\":\"img_1\",\"kind\":\"image\",\"query\":\"Elon xAI Colossus supercomputer image\","
                    "\"caption\":\"Colossus infrastructure\"}]]\n\n"
                    "The scale matters because it compresses training timelines."
                )
            )

            final_payload = await coordinator.finalize()

        image_block = next(
            block for block in final_payload["response_blocks"] if block["type"] == "image_artifact"
        )
        assert image_block["provenance"]["source_image_url"] == "https://cdn.example.test/usable-colossus.png"
        assert len(final_payload["supporting_artifacts"]) == 1
        assert final_payload["supporting_artifacts"][0]["source_image_url"] == (
            "https://cdn.example.test/usable-colossus.png"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_skips_tiny_image_and_uses_larger_candidate() -> None:
    root = _make_test_dir("visual-image-small-")
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
            visual_firecrawl_api_key="firecrawl-key",
            visual_image_search_enabled=False,
        )
        transport = _build_tiny_image_retry_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_small_1",
                request_id="req_visual_image_small_1",
                session_id="sess_visual_image_small_1",
                channel="desktop:desk_visual_image_small_1",
                user_query="Tell me why Cursor benefits from Colossus. Include inline images.",
                http_client=client,
            )
            _note_colossus_source(coordinator)

            coordinator.consume_text(
                (
                    "Cursor benefits because Colossus removes its training bottleneck.\n\n"
                    "[[visual_slot {\"id\":\"img_1\",\"kind\":\"image\",\"query\":\"Cursor compute bottleneck and Colossus training\","
                    "\"caption\":\"Infrastructure that removes Cursor's compute bottleneck\"}]]\n\n"
                    "That shifts Cursor from being compute-constrained to compute-rich."
                )
            )

            final_payload = await coordinator.finalize()

        image_block = next(
            block for block in final_payload["response_blocks"] if block["type"] == "image_artifact"
        )
        assert image_block["provenance"]["source_image_url"] == "https://cdn.example.test/colossus-racks.png"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_finalize_waits_for_slot_timeout_budget() -> None:
    root = _make_test_dir("visual-image-finalize-")
    try:
        config = OrchestratorConfig(
            artifacts_root=root / "artifacts",
            internal_token="internal-token",
            signing_secret="signing-secret",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-opus-4-6",
            task_ledger_db_path=root / "task_ledger.db",
            visual_enhancement_enabled=True,
            visual_finalization_grace_ms=100,
            visual_image_slot_timeout_ms=1200,
            visual_max_concurrent_sidecars=1,
            visual_max_image_slots_per_turn=1,
            visual_firecrawl_api_key="firecrawl-key",
            visual_image_search_enabled=False,
        )
        transport = _build_delayed_image_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_finalize_1",
                request_id="req_visual_image_finalize_1",
                session_id="sess_visual_image_finalize_1",
                channel="desktop:desk_visual_image_finalize_1",
                user_query="Why didn't OpenAI buy Cursor? Include inline images.",
                http_client=client,
            )
            _note_colossus_source(coordinator)

            coordinator.consume_text(
                (
                    "OpenAI had strategic conflicts here.\n\n"
                    "[[visual_slot {\"id\":\"img_1\",\"kind\":\"image\",\"query\":\"Colossus facility image\","
                    "\"caption\":\"Colossus facility\"}]]\n\n"
                    "SpaceX could offer both capital and compute."
                )
            )

            started = time.perf_counter()
            final_payload = await coordinator.finalize()
            elapsed = time.perf_counter() - started

        image_block = next(
            block for block in final_payload["response_blocks"] if block["type"] == "image_artifact"
        )
        assert image_block["provenance"]["source_image_url"] == "https://cdn.example.test/colossus-facility.png"
        assert elapsed >= 0.35
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_filters_svg_ui_noise_before_ranking() -> None:
    root = _make_test_dir("visual-image-svg-noise-")
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
            visual_firecrawl_api_key="firecrawl-key",
            visual_image_search_enabled=False,
        )
        transport = _build_svg_noise_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_svg_noise_1",
                request_id="req_visual_image_svg_noise_1",
                session_id="sess_visual_image_svg_noise_1",
                channel="desktop:desk_visual_image_svg_noise_1",
                user_query="How do companies think like this? Include inline images.",
                http_client=client,
            )
            _note_strategy_source(coordinator)

            coordinator.consume_text(
                (
                    "The best companies think in systems.\n\n"
                    "[[visual_slot {\"id\":\"img_1\",\"kind\":\"image\",\"query\":\"company strategy and AI infrastructure\","
                    "\"caption\":\"Infrastructure and strategy\"}]]\n\n"
                    "They optimize for strategic control, not just immediate revenue."
                )
            )

            final_payload = await coordinator.finalize()

        image_block = next(
            block for block in final_payload["response_blocks"] if block["type"] == "image_artifact"
        )
        assert image_block["provenance"]["source_image_url"] == "https://cdn.example.test/colossus-strategy.png"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_direct_image_search_client_parses_bing_result_payloads() -> None:
    html_payload = """
    <html><body>
      <a class="iusc" m='{"murl":"https://cdn.example.test/black-flag-keyart.jpg","purl":"https://news.ubisoft.com/en-us/article/black-flag-resynced","t":"Assassin\\u0027s Creed Black Flag Resynced key art","desc":"Official reveal artwork","imgw":1600,"imgh":900}'></a>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url).startswith("https://www.bing.com/images/search"):
            return httpx.Response(200, text=html_payload, headers={"Content-Type": "text/html"})
        raise AssertionError(f"unexpected request {request.method} {request.url!s}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        search_client = DirectImageSearchClient(
            DirectImageSearchConfig(
                enabled=True,
                base_url="https://www.bing.com/images/search",
                timeout_sec=12.0,
                result_limit=8,
            ),
            http_client=client,
        )
        results = await search_client.search_images("Assassin's Creed Black Flag Resynced key art")

    assert len(results) == 1
    assert results[0]["image_url"] == "https://cdn.example.test/black-flag-keyart.jpg"
    assert results[0]["source_url"] == "https://news.ubisoft.com/en-us/article/black-flag-resynced"
    assert results[0]["title"] == "Assassin's Creed Black Flag Resynced key art"
    assert results[0]["width"] == 1600
    assert results[0]["height"] == 900


@pytest.mark.asyncio
async def test_visual_enrichment_explicit_request_uses_direct_image_search_when_trusted_image_is_cross_promo() -> None:
    root = _make_test_dir("visual-image-search-fallback-")
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
            visual_firecrawl_api_key="firecrawl-key",
            visual_image_search_enabled=True,
            visual_image_search_base_url="https://www.bing.com/images/search",
        )
        transport = _build_cross_promo_with_search_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_search_fallback_1",
                request_id="req_visual_image_search_fallback_1",
                session_id="sess_visual_image_search_fallback_1",
                channel="desktop:desk_visual_image_search_fallback_1",
                user_query="Tell me more about Assassin's Creed Black Flag Resynced. Include inline images.",
                http_client=client,
            )
            _note_black_flag_source(coordinator)

            coordinator.consume_text(
                (
                    "Here's the full deep dive on the remake.\n\n"
                    "[[visual_slot {\"id\":\"img_1\",\"kind\":\"image\",\"query\":\"Assassin's Creed Black Flag Resynced official key art\","
                    "\"caption\":\"Official key art\"}]]\n\n"
                    "The remake modernizes combat, stealth, and naval systems."
                )
            )

            final_payload = await coordinator.finalize()

        image_block = next(
            block for block in final_payload["response_blocks"] if block["type"] == "image_artifact"
        )
        assert image_block["provenance"]["source_image_url"].endswith(
            "/ACBFR_KeyArt_StandardEdition_960x540_23.04.26_615PMCEST.jpg"
        )
        assert image_block["provenance"]["selection_reason"] == (
            "Best metadata-ranked image from direct image-search results."
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_uses_source_title_queries_for_vague_image_requests() -> None:
    root = _make_test_dir("visual-image-query-enrichment-")
    try:
        config = OrchestratorConfig(
            internal_token="internal-token",
            signing_secret="signing-secret",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-opus-4-6",
            task_ledger_db_path=root / "task_ledger.db",
            visual_enhancement_enabled=True,
            visual_finalization_grace_ms=1200,
            visual_max_concurrent_sidecars=1,
            visual_max_image_slots_per_turn=1,
            visual_firecrawl_api_key="",
            visual_image_search_enabled=True,
            visual_image_search_base_url="https://www.bing.com/images/search",
        )
        transport = _build_source_title_query_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_query_enrichment_1",
                request_id="req_visual_image_query_enrichment_1",
                session_id="sess_visual_image_query_enrichment_1",
                channel="desktop:desk_visual_image_query_enrichment_1",
                user_query="What is this bus complaint!? include inline images!",
                http_client=client,
            )
            _note_bus_complaint_source(coordinator)

            coordinator.consume_text(
                (
                    "Here's the full story.\n\n"
                    "[[visual_slot {\"id\":\"img_1\",\"kind\":\"image\",\"query\":\"What is this bus complaint?\","
                    "\"caption\":\"Vijay's bus-terminal complaint\"}]]\n\n"
                    "Vijay said stranded voters could miss polling because of transport chaos."
                )
            )

            final_payload = await coordinator.finalize()

        image_block = next(
            block for block in final_payload["response_blocks"] if block["type"] == "image_artifact"
        )
        assert image_block["provenance"]["source_image_url"] == "https://cdn.example.test/vijay-bus-complaint.jpg"
        assert "direct image-search results" in image_block["provenance"]["selection_reason"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_visual_enrichment_auto_injects_image_slot_for_concrete_follow_up() -> None:
    root = _make_test_dir("visual-image-auto-slot-")
    try:
        config = OrchestratorConfig(
            internal_token="internal-token",
            signing_secret="signing-secret",
            anthropic_api_key="anthropic-key",
            anthropic_model="claude-opus-4-6",
            task_ledger_db_path=root / "task_ledger.db",
            visual_enhancement_enabled=True,
            visual_finalization_grace_ms=1200,
            visual_max_concurrent_sidecars=1,
            visual_max_image_slots_per_turn=1,
            visual_firecrawl_api_key="",
            visual_image_search_enabled=True,
            visual_image_search_base_url="https://www.bing.com/images/search",
        )
        transport = _build_auto_slot_query_transport()

        async with httpx.AsyncClient(transport=transport) as client:
            coordinator = VisualEnrichmentCoordinator(
                config=config,
                task_id="tsk_visual_image_auto_slot_1",
                request_id="req_visual_image_auto_slot_1",
                session_id="sess_visual_image_auto_slot_1",
                channel="desktop:desk_visual_image_auto_slot_1",
                user_query="complete it",
                http_client=client,
            )
            _note_macrohard_source(coordinator)

            visible_delta, snapshot_events = coordinator.consume_text(
                (
                    "My bad — I answered about the wrong company entirely. Macrohard = xAI. "
                    "The real story is that xAI is clustering multiple Memphis-area buildings, "
                    "using the Tennessee-Mississippi border as an execution advantage, and tying "
                    "that infrastructure expansion directly to the company’s broader software and "
                    "model ambitions."
                )
            )

            assert visible_delta
            assert snapshot_events

            final_payload = await coordinator.finalize()

        image_block = next(
            block for block in final_payload["response_blocks"] if block["type"] == "image_artifact"
        )
        assert image_block["provenance"]["source_image_url"] == "https://cdn.example.test/xai-macrohard-campus.jpg"
        assert len(final_payload["supporting_artifacts"]) == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_visual_enrichment_flags_text_dominant_word_art() -> None:
    assert _is_probably_text_art(
        "https://x.ai/_next/static/media/colossus-text.8af37456.webp",
        "colossus-text.8af37456.webp",
        "Inside xAI's Colossus supercomputer GPU racks in Memphis",
    )
    assert not _is_probably_text_art(
        "https://x.ai/_next/static/media/colossus-racks.8af37456.webp",
        "colossus-racks.8af37456.webp",
        "Inside xAI's Colossus supercomputer GPU racks in Memphis",
    )


def test_render_chart_png_uses_high_resolution_dark_theme() -> None:
    spec = normalize_chart_spec(
        {
            "chart_type": "line",
            "title": "Single-site AI clusters",
            "x_label": "Cluster",
            "y_label": "GPUs",
            "series": [
                {
                    "label": "GPU count",
                    "points": [
                        {"x": "El Capitan", "y": 44544},
                        {"x": "Oracle", "y": 131072},
                        {"x": "Colossus", "y": 555000},
                    ],
                }
            ],
        },
        max_points=20,
    )
    image_bytes = render_chart_png(spec)
    assert len(image_bytes) > 10_000
    with Image.open(BytesIO(image_bytes)) as image:
        assert image.size == (1600, 900)
