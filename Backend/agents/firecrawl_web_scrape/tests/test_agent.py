from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agents.firecrawl_web_scrape.agent import FirecrawlWebScrapeAgent
from agents.firecrawl_web_scrape.config import FirecrawlWebScrapeConfig
from shared import TaskEnvelope, sign_task_envelope, utcnow


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.lists: dict[str, list[str]] = {}
        self.expirations: dict[str, int] = {}
        self._counter = 0
        self._sequence = 0

    async def incr(self, key: str) -> int:
        self._counter += 1
        return self._counter

    async def xadd(
        self,
        stream: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool | None = None,
    ) -> str:
        del maxlen, approximate
        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self.streams.setdefault(stream, []).append((message_id, dict(fields)))
        return message_id

    async def rpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)

    async def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True


def _make_task(*, intent: str, payload: dict[str, object], session_id: str = "sess_firecrawl") -> TaskEnvelope:
    task = TaskEnvelope(
        task_id=f"tsk_{intent.replace('.', '_')}",
        task_list_id=session_id,
        parent_task_id="tsk_parent",
        session_id=session_id,
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/firecrawl-web-scrape-agent:1.0.0",
        intent=intent,
        input=payload,
        input_artifacts=[],
        idempotency_key=f"idem_{intent.replace('.', '_')}",
        priority="normal",
        signature="",
        created_at=utcnow(),
        source="user",
        source_id="desktop",
        channel="desktop:test",
    )
    return task.model_copy(update={"signature": sign_task_envelope(task, "agent-secret")})


@pytest.mark.asyncio
async def test_firecrawl_agent_scrape_persists_artifacts_and_compact_output(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.firecrawl.dev/v2/scrape")
        assert request.headers["Authorization"] == "Bearer firecrawl-key"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["url"] == "https://example.com/post"
        assert payload["formats"] == ["markdown", "links"]
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "# Example\n\n" + ("A" * 5000),
                    "links": ["https://example.com/a", "https://example.com/b"],
                    "metadata": {"title": "Example Post", "language": "en"},
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as firecrawl_client:
        agent = FirecrawlWebScrapeAgent(
            redis_client=FakeRedis(),
            config=FirecrawlWebScrapeConfig(
                redis_url="redis://unused",
                gateway_url="http://gateway",
                gateway_internal_token="internal-token",
                firecrawl_api_key="firecrawl-key",
                firecrawl_api_base_url="https://api.firecrawl.dev",
            ),
            firecrawl_client=firecrawl_client,
            store_root=tmp_path / "store",
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )
        await agent.on_startup()
        try:
            result = await agent.execute(
                _make_task(
                    intent="firecrawl.scrape",
                    payload={
                        "url": "https://example.com/post",
                        "formats": ["markdown", "links"],
                    },
                )
            )
        finally:
            await agent.stop()

    assert result.status == "completed"
    assert result.output["url"] == "https://example.com/post"
    assert result.output["title"] == "Example Post"
    assert result.output["available_formats"] == ["markdown", "links"]
    assert result.output["data"]["links_count"] == 2
    # 5011-char markdown is under the default inline budget, so it is surfaced whole
    # and not flagged as truncated.
    assert len(result.output["data"]["markdown_excerpt"]) == 5011
    assert "markdown_truncated" not in result.output["data"]
    assert len(result.artifacts) == 3
    artifact_paths = {artifact.path for artifact in result.artifacts}
    assert any(path.endswith("runs/artifacts/tsk_firecrawl_scrape/firecrawl_web_scrape/scrape_response.json") for path in artifact_paths)
    assert any(path.endswith("runs/artifacts/tsk_firecrawl_scrape/firecrawl_web_scrape/page.md") for path in artifact_paths)
    assert any(path.endswith("runs/artifacts/tsk_firecrawl_scrape/firecrawl_web_scrape/links.json") for path in artifact_paths)


@pytest.mark.asyncio
async def test_firecrawl_agent_scrape_screenshot_becomes_vision_image_artifact(tmp_path: Path) -> None:
    screenshot_url = "https://storage.firecrawl.dev/screenshots/shot123.png"
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL("https://api.firecrawl.dev/v2/scrape"):
            payload = json.loads(request.content.decode("utf-8"))
            # Screenshots are requested in object form with full-page capture by default.
            assert payload["formats"][0]["type"] == "screenshot"
            assert payload["formats"][0]["fullPage"] is True
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "screenshot": screenshot_url,
                        "metadata": {"title": "Benchmark Page"},
                    },
                },
            )
        if request.url == httpx.URL(screenshot_url):
            return httpx.Response(200, content=png_bytes, headers={"content-type": "image/png"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as firecrawl_client:
        agent = FirecrawlWebScrapeAgent(
            redis_client=FakeRedis(),
            config=FirecrawlWebScrapeConfig(
                redis_url="redis://unused",
                gateway_url="http://gateway",
                gateway_internal_token="internal-token",
                firecrawl_api_key="firecrawl-key",
                firecrawl_api_base_url="https://api.firecrawl.dev",
            ),
            firecrawl_client=firecrawl_client,
            store_root=tmp_path / "store",
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )
        await agent.on_startup()
        try:
            result = await agent.execute(
                _make_task(
                    intent="firecrawl.scrape",
                    payload={"url": "https://example.com/benchmarks", "formats": ["screenshot"]},
                )
            )
        finally:
            await agent.stop()

    assert result.status == "completed"
    assert "screenshot" in result.output["available_formats"]
    assert result.output["data"]["screenshot_url"] == screenshot_url
    # A real image/png artifact is persisted so the orchestrator vision path can read it.
    image_manifests = [a for a in result.artifacts if a.mime == "image/png"]
    assert len(image_manifests) == 1
    assert image_manifests[0].path.endswith("/firecrawl_web_scrape/screenshot.png")
    # The model-facing artifact ref advertises an image with a fetchable URL, which is
    # what triggers the orchestrator's image_url -> vision escalation.
    image_refs = [ref for ref in result.output["artifacts"] if ref.get("mime") == "image/png"]
    assert len(image_refs) == 1
    assert image_refs[0]["download_url"] == screenshot_url
    assert image_refs[0]["filename"] == "screenshot.png"


@pytest.mark.asyncio
async def test_firecrawl_agent_scrape_direct_image_url_fetches_vision_artifact(tmp_path: Path) -> None:
    image_url = "https://sakana.ai/assets/fugu-release/benchmark-table.png"
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"7" * 128

    async def handler(request: httpx.Request) -> httpx.Response:
        # Firecrawl's /v2/scrape must NOT be called for a direct image URL.
        assert request.url.path != "/v2/scrape"
        if request.url == httpx.URL(image_url):
            return httpx.Response(200, content=png_bytes, headers={"content-type": "image/png"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as firecrawl_client:
        agent = FirecrawlWebScrapeAgent(
            redis_client=FakeRedis(),
            config=FirecrawlWebScrapeConfig(
                redis_url="redis://unused",
                gateway_url="http://gateway",
                gateway_internal_token="internal-token",
                firecrawl_api_key="firecrawl-key",
                firecrawl_api_base_url="https://api.firecrawl.dev",
            ),
            firecrawl_client=firecrawl_client,
            store_root=tmp_path / "store",
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )
        await agent.on_startup()
        try:
            result = await agent.execute(
                _make_task(intent="firecrawl.scrape", payload={"url": image_url})
            )
        finally:
            await agent.stop()

    assert result.status == "completed"
    assert result.output["available_formats"] == ["image"]
    image_manifests = [a for a in result.artifacts if a.mime == "image/png"]
    assert len(image_manifests) == 1
    assert image_manifests[0].path.endswith("/firecrawl_web_scrape/benchmark-table.png")
    image_refs = [ref for ref in result.output["artifacts"] if ref.get("mime") == "image/png"]
    assert len(image_refs) == 1
    assert image_refs[0]["download_url"] == image_url


@pytest.mark.asyncio
async def test_firecrawl_agent_scrape_falls_back_to_image_fetch_on_content_type_error(tmp_path: Path) -> None:
    # URL has no image extension, so Firecrawl is tried first and rejects it as an image.
    target_url = "https://example.com/asset?id=benchmark"
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"9" * 64

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/scrape":
            return httpx.Response(
                400,
                json={
                    "success": False,
                    "error": "The URL returned a file type that Firecrawl cannot process: image/png.",
                },
            )
        if request.url == httpx.URL(target_url):
            return httpx.Response(200, content=png_bytes, headers={"content-type": "image/png"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as firecrawl_client:
        agent = FirecrawlWebScrapeAgent(
            redis_client=FakeRedis(),
            config=FirecrawlWebScrapeConfig(
                redis_url="redis://unused",
                gateway_url="http://gateway",
                gateway_internal_token="internal-token",
                firecrawl_api_key="firecrawl-key",
                firecrawl_api_base_url="https://api.firecrawl.dev",
            ),
            firecrawl_client=firecrawl_client,
            store_root=tmp_path / "store",
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )
        await agent.on_startup()
        try:
            result = await agent.execute(
                _make_task(intent="firecrawl.scrape", payload={"url": target_url, "formats": ["markdown"]})
            )
        finally:
            await agent.stop()

    assert result.status == "completed"
    assert result.output["available_formats"] == ["image"]
    assert any(a.mime == "image/png" for a in result.artifacts)


@pytest.mark.asyncio
async def test_firecrawl_agent_scrape_truncates_long_markdown_with_pointer(tmp_path: Path) -> None:
    long_markdown = "# Title\n\n" + ("X" * 6000)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.firecrawl.dev/v2/scrape")
        return httpx.Response(
            200,
            json={"success": True, "data": {"markdown": long_markdown, "metadata": {}}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as firecrawl_client:
        agent = FirecrawlWebScrapeAgent(
            redis_client=FakeRedis(),
            config=FirecrawlWebScrapeConfig(
                redis_url="redis://unused",
                gateway_url="http://gateway",
                gateway_internal_token="internal-token",
                firecrawl_api_key="firecrawl-key",
                firecrawl_api_base_url="https://api.firecrawl.dev",
                inline_markdown_chars=2000,
            ),
            firecrawl_client=firecrawl_client,
            store_root=tmp_path / "store",
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )
        await agent.on_startup()
        try:
            result = await agent.execute(
                _make_task(intent="firecrawl.scrape", payload={"url": "https://example.com/long"})
            )
        finally:
            await agent.stop()

    data = result.output["data"]
    assert len(data["markdown_excerpt"]) <= 2000
    assert data["markdown_truncated"] is True
    assert data["markdown_full_chars"] == len(long_markdown)
    assert data["markdown_full_artifact"] == "page.md"


@pytest.mark.asyncio
async def test_firecrawl_agent_scrape_forwards_parsers(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.firecrawl.dev/v2/scrape")
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["parsers"] == ["pdf"]
        return httpx.Response(
            200,
            json={"success": True, "data": {"markdown": "parsed pdf text", "metadata": {}}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as firecrawl_client:
        agent = FirecrawlWebScrapeAgent(
            redis_client=FakeRedis(),
            config=FirecrawlWebScrapeConfig(
                redis_url="redis://unused",
                gateway_url="http://gateway",
                gateway_internal_token="internal-token",
                firecrawl_api_key="firecrawl-key",
                firecrawl_api_base_url="https://api.firecrawl.dev",
            ),
            firecrawl_client=firecrawl_client,
            store_root=tmp_path / "store",
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )
        await agent.on_startup()
        try:
            result = await agent.execute(
                _make_task(
                    intent="firecrawl.scrape",
                    payload={"url": "https://example.com/doc.pdf", "parsers": ["pdf"]},
                )
            )
        finally:
            await agent.stop()

    assert result.status == "completed"


@pytest.mark.asyncio
async def test_firecrawl_agent_extract_polls_and_recall_session_reads_private_ledger(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v2/extract":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["urls"] == ["https://example.com/a", "https://example.com/b"]
            assert payload["showSources"] is True
            assert payload["scrapeOptions"]["onlyMainContent"] is True
            assert payload["scrapeOptions"]["parsers"] == ["pdf"]
            # Anti-hallucination guard is appended to the Firecrawl extract prompt.
            assert "never guess" in payload["prompt"].lower()
            return httpx.Response(
                200,
                json={"success": True, "id": "job_123", "status": "processing", "invalidURLs": ["https://bad.local"]},
            )
        if request.url.path == "/v2/extract/job_123":
            if calls.count("GET /v2/extract/job_123") == 1:
                return httpx.Response(200, json={"success": True, "id": "job_123", "status": "processing"})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "id": "job_123",
                    "status": "completed",
                    "data": [{"company": "Cosmic", "score": 0.98}],
                    "sources": [{"url": "https://example.com/a", "title": "Source A"}],
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as firecrawl_client:
        agent = FirecrawlWebScrapeAgent(
            redis_client=FakeRedis(),
            config=FirecrawlWebScrapeConfig(
                redis_url="redis://unused",
                gateway_url="http://gateway",
                gateway_internal_token="internal-token",
                firecrawl_api_key="firecrawl-key",
                firecrawl_api_base_url="https://api.firecrawl.dev",
                firecrawl_extract_poll_interval_sec=0.01,
                firecrawl_extract_max_wait_sec=5.0,
            ),
            firecrawl_client=firecrawl_client,
            store_root=tmp_path / "store",
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )
        await agent.on_startup()
        try:
            extract_result = await agent.execute(
                _make_task(
                    intent="firecrawl.extract",
                    payload={
                        "urls": ["https://example.com/a", "https://example.com/b"],
                        "prompt": "Extract the company names and confidence scores.",
                        "show_sources": True,
                        "parsers": ["pdf"],
                    },
                )
            )
            recall_result = await agent.execute(
                _make_task(
                    intent="firecrawl.recall_session",
                    payload={"session_id": "sess_firecrawl", "limit": 5},
                )
            )
        finally:
            await agent.stop()

    assert extract_result.status == "completed"
    assert extract_result.output["job_id"] == "job_123"
    assert extract_result.output["status"] == "completed"
    assert extract_result.output["invalid_urls"] == ["https://bad.local"]
    assert extract_result.output["data"] == {"items": [{"company": "Cosmic", "score": 0.98}]}
    assert extract_result.output["sources"][0]["title"] == "Source A"
    assert len(extract_result.artifacts) >= 3

    assert recall_result.status == "completed"
    assert recall_result.output["session_id"] == "sess_firecrawl"
    assert len(recall_result.output["entries"]) == 1
    assert recall_result.output["entries"][0]["intent"] == "firecrawl.extract"
    assert recall_result.output["entries"][0]["artifact_refs"]


@pytest.mark.asyncio
async def test_firecrawl_agent_rejects_invalid_proxy_and_missing_session_id(tmp_path: Path) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as firecrawl_client:
        agent = FirecrawlWebScrapeAgent(
            redis_client=FakeRedis(),
            config=FirecrawlWebScrapeConfig(
                redis_url="redis://unused",
                gateway_url="http://gateway",
                gateway_internal_token="internal-token",
                firecrawl_api_key="firecrawl-key",
                firecrawl_api_base_url="https://api.firecrawl.dev",
            ),
            firecrawl_client=firecrawl_client,
            store_root=tmp_path / "store",
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )
        await agent.on_startup()
        try:
            scrape_result = await agent.execute(
                _make_task(
                    intent="firecrawl.scrape",
                    payload={
                        "url": "https://example.com/post",
                        "proxy": "stealth",
                    },
                )
            )
            recall_result = await agent.execute(
                _make_task(
                    intent="firecrawl.recall_session",
                    payload={},
                )
            )
        finally:
            await agent.stop()

    assert scrape_result.status == "failed"
    assert scrape_result.error is not None
    assert scrape_result.error.code == "INVALID_INPUT"
    assert "proxy" in scrape_result.error.message
    assert recall_result.status == "failed"
    assert recall_result.error is not None
    assert recall_result.error.code == "INVALID_INPUT"
