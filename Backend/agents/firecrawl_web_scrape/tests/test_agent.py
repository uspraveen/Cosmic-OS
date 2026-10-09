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
    for ref in result.output["artifacts"]:
        assert ref.get("audience") == "supporting"
        assert ref.get("filename")


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
    # The full-page screenshot is a vision aid only: it must NOT be surfaced as an inline
    # image / Produced Files deliverable card, so it is marked "supporting". The
    # provider_url/download_url it still carries is what feeds the vision model.
    assert image_refs[0]["kind"] == "screenshot"
    assert image_refs[0]["audience"] == "supporting"
    assert image_refs[0]["provider_url"] == screenshot_url


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
    assert image_refs[0]["audience"] == "deliverable"


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


@pytest.mark.asyncio
async def test_firecrawl_agent_search_returns_results_tools_and_contract(tmp_path: Path) -> None:
    long_description = "d" * 800
    contract = {
        "query": {"type": "string"},
        "k": {"type": "integer"},
        "authors": {"type": "array"},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.firecrawl.dev/v2/search")
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["query"] == "retrieval augmented generation benchmarks"
        assert payload["sources"] == ["web", "alexandria"]
        assert payload["toolDetail"] == "full"
        assert payload["limit"] == 5
        assert payload["categories"] == ["research"]
        assert payload["tbs"] == "qdr:y"
        return httpx.Response(
            200,
            json={
                "success": True,
                "id": "srch_1",
                "creditsUsed": 2,
                "data": {
                    "web": [
                        {
                            "title": "RAGBench",
                            "url": "https://arxiv.org/abs/2407.11005",
                            "description": long_description,
                        }
                    ],
                    "tools": [
                        {
                            "provider": "firecrawl-research-index",
                            "capability": "search",
                            "description": "Find scientific papers about a research question.",
                            "options": contract,
                        }
                    ],
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
                    intent="firecrawl.search",
                    payload={
                        "query": "retrieval augmented generation benchmarks",
                        "sources": ["web", "alexandria"],
                        "categories": ["research"],
                        "limit": 5,
                        "tbs": "qdr:y",
                        "tool_detail": "full",
                    },
                )
            )
        finally:
            await agent.stop()

    assert result.status == "completed"
    assert result.output["query"] == "retrieval augmented generation benchmarks"
    assert result.output["web"][0]["url"] == "https://arxiv.org/abs/2407.11005"
    # Descriptions are clipped to the inline budget.
    assert len(result.output["web"][0]["description"]) == 600
    # Tool contracts arrive intact so firecrawl.alexandria can be built from them.
    assert result.output["tools"][0]["provider"] == "firecrawl-research-index"
    assert result.output["tools"][0]["options"] == contract
    assert result.output["credits_used"] == 2
    assert any(a.path.endswith("search_response.json") for a in result.artifacts)


@pytest.mark.asyncio
async def test_firecrawl_agent_alexandria_executes_contract_and_tracks_spend(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.firecrawl.dev/v2/scrape")
        payload = json.loads(request.content.decode("utf-8"))
        # Alexandria executions carry an alexandria payload, never a url.
        assert "url" not in payload
        assert payload["alexandria"]["provider"] == "firecrawl-research-index"
        assert payload["alexandria"]["capability"] == "search"
        assert payload["alexandria"]["options"] == {"query": "rag evaluation", "k": 2}
        return httpx.Response(
            200,
            json={
                "success": True,
                "scrape_id": "scr_1",
                "data": {
                    "alexandria": [
                        {
                            "provider": "firecrawl-research-index",
                            "capability": "search",
                            "creditsCost": 0,
                            "data": {
                                "results": [
                                    {"paperId": "1", "title": "RAGBench"},
                                    {"paperId": "2", "title": "MIRAGE"},
                                ]
                            },
                            "records": 2,
                            "upstreamStatus": 200,
                            "alexandriaId": "alex_1",
                            "providerRequestId": "prov_1",
                        }
                    ]
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
                    intent="firecrawl.alexandria",
                    payload={
                        "provider": "firecrawl-research-index",
                        "capability": "search",
                        "options": {"query": "rag evaluation", "k": 2},
                    },
                )
            )
        finally:
            await agent.stop()

    assert result.status == "completed"
    output = result.output
    assert output["success"] is True
    assert output["provider_error"] is None
    assert output["records_count"] == 2
    assert output["data"] == {"results": [{"paperId": "1", "title": "RAGBench"}, {"paperId": "2", "title": "MIRAGE"}]}
    assert output["data_truncated"] is False
    assert output["credits_cost"] == 0
    assert output["task_credits_spent"] == 0
    assert output["task_credit_cap"] == 40
    assert output["budget_exceeded"] is False
    assert output["alexandria_id"] == "alex_1"
    artifact_paths = {a.path for a in result.artifacts}
    assert any(path.endswith("alexandria_response.json") for path in artifact_paths)
    assert any(path.endswith("alexandria_records.json") for path in artifact_paths)


@pytest.mark.asyncio
async def test_firecrawl_agent_alexandria_budget_cap_blocks_further_calls(tmp_path: Path) -> None:
    scrape_calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        scrape_calls["count"] += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "alexandria": [
                        {
                            "provider": "semanticscholar-org",
                            "capability": "papers/search_papers",
                            "creditsCost": 5,
                            "data": {"papers": [{"paper_id": "p1"}]},
                        }
                    ]
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
                alexandria_task_credit_cap=5,
            ),
            firecrawl_client=firecrawl_client,
            store_root=tmp_path / "store",
            runtime_root=tmp_path / "runtime",
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )
        await agent.on_startup()
        try:
            first = await agent.execute(
                _make_task(
                    intent="firecrawl.alexandria",
                    payload={"provider": "semanticscholar-org", "capability": "papers/search_papers"},
                )
            )
            second = await agent.execute(
                _make_task(
                    intent="firecrawl.alexandria",
                    payload={"provider": "semanticscholar-org", "capability": "papers/search_papers"},
                )
            )
        finally:
            await agent.stop()

    # Exactly one paid call may hit the network; the second is refused locally.
    assert scrape_calls["count"] == 1
    assert first.status == "completed"
    assert first.output["credits_cost"] == 5
    assert first.output["budget_exceeded"] is True
    assert "cap reached" in first.output["response"]
    assert second.status == "failed"
    assert second.error is not None
    assert second.error.code == "BUDGET_EXCEEDED"
    assert "5/5" in second.error.message


@pytest.mark.asyncio
async def test_firecrawl_agent_alexandria_budget_survives_agent_restart(tmp_path: Path) -> None:
    # Spend is summed from the session ledger, so a fresh agent instance (simulating
    # a process restart mid-task) still sees what the previous instance spent.
    scrape_calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        scrape_calls["count"] += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "alexandria": [
                        {
                            "provider": "semanticscholar-org",
                            "capability": "papers/search_papers",
                            "creditsCost": 5,
                            "data": {"papers": [{"paper_id": "p1"}]},
                        }
                    ]
                },
            },
        )

    transport = httpx.MockTransport(handler)
    store_root = tmp_path / "store"
    runtime_root = tmp_path / "runtime"

    def build_agent() -> FirecrawlWebScrapeAgent:
        return FirecrawlWebScrapeAgent(
            redis_client=FakeRedis(),
            config=FirecrawlWebScrapeConfig(
                redis_url="redis://unused",
                gateway_url="http://gateway",
                gateway_internal_token="internal-token",
                firecrawl_api_key="firecrawl-key",
                firecrawl_api_base_url="https://api.firecrawl.dev",
                alexandria_task_credit_cap=5,
            ),
            firecrawl_client=httpx.AsyncClient(transport=transport),
            store_root=store_root,
            runtime_root=runtime_root,
            artifacts_root=tmp_path / "runs" / "artifacts",
            agent_secret="agent-secret",
        )

    first_agent = build_agent()
    await first_agent.on_startup()
    try:
        first = await first_agent.execute(
            _make_task(
                intent="firecrawl.alexandria",
                payload={"provider": "semanticscholar-org", "capability": "papers/search_papers"},
            )
        )
    finally:
        await first_agent.stop()
    assert first.status == "completed"

    second_agent = build_agent()
    await second_agent.on_startup()
    try:
        second = await second_agent.execute(
            _make_task(
                intent="firecrawl.alexandria",
                payload={"provider": "semanticscholar-org", "capability": "papers/search_papers"},
            )
        )
    finally:
        await second_agent.stop()

    assert scrape_calls["count"] == 1
    assert second.status == "failed"
    assert second.error is not None
    assert second.error.code == "BUDGET_EXCEEDED"
    assert "5/5" in second.error.message


@pytest.mark.asyncio
async def test_firecrawl_agent_alexandria_surfaces_per_result_errors(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        tool = payload["alexandria"]
        if tool["capability"] == "podcasts/episodes/search":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "alexandria": [
                            {
                                "provider": "particle",
                                "capability": "podcasts/episodes/search",
                                "creditsCost": 0,
                                "error": {
                                    "code": "THIRD_PARTY_DATA_TERMS_REQUIRED",
                                    "message": "Terms acceptance is required for this provider.",
                                    "status": 400,
                                    "requiresAction": {"url": "https://www.firecrawl.dev/app/alexandria/terms"},
                                },
                            }
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "alexandria": [
                        {
                            "provider": "firecrawl-research-index",
                            "capability": "search",
                            "creditsCost": 0,
                            "error": {
                                "code": "invalid_option",
                                "message": "search does not take limit. Valid options: query, k, authors.",
                                "status": 400,
                            },
                        }
                    ]
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
            terms = await agent.execute(
                _make_task(
                    intent="firecrawl.alexandria",
                    payload={"provider": "particle", "capability": "podcasts/episodes/search"},
                )
            )
            invalid = await agent.execute(
                _make_task(
                    intent="firecrawl.alexandria",
                    payload={"provider": "firecrawl-research-index", "capability": "search"},
                )
            )
        finally:
            await agent.stop()

    # The HTTP call succeeded, so the result completes but must not read as data.
    assert terms.status == "completed"
    assert terms.output["success"] is False
    assert terms.output["provider_error"]["code"] == "THIRD_PARTY_DATA_TERMS_REQUIRED"
    assert terms.output["requires_action_url"] == "https://www.firecrawl.dev/app/alexandria/terms"
    assert "terms" in terms.output["response"]

    assert invalid.status == "completed"
    assert invalid.output["success"] is False
    assert "Valid options: query, k, authors" in invalid.output["provider_error"]["message"]


@pytest.mark.asyncio
async def test_firecrawl_agent_search_and_alexandria_validate_input(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No network call is expected for invalid input.")

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
            empty_query = await agent.execute(
                _make_task(intent="firecrawl.search", payload={"query": "  "})
            )
            bad_source = await agent.execute(
                _make_task(intent="firecrawl.search", payload={"query": "test", "sources": ["tiktok"]})
            )
            gov_combo = await agent.execute(
                _make_task(intent="firecrawl.search", payload={"query": "test", "categories": ["gov", "research"]})
            )
            domain_conflict = await agent.execute(
                _make_task(
                    intent="firecrawl.search",
                    payload={
                        "query": "test",
                        "include_domains": ["sec.gov"],
                        "exclude_domains": ["reddit.com"],
                    },
                )
            )
            missing_capability = await agent.execute(
                _make_task(intent="firecrawl.alexandria", payload={"provider": "sec-gov"})
            )
            bad_options = await agent.execute(
                _make_task(
                    intent="firecrawl.alexandria",
                    payload={"provider": "sec-gov", "capability": "filings/company", "options": ["bad"]},
                )
            )
        finally:
            await agent.stop()

    for result in (empty_query, bad_source, gov_combo, domain_conflict, missing_capability, bad_options):
        assert result.status == "failed", result
        assert result.error is not None
        assert result.error.code == "INVALID_INPUT", result.error
