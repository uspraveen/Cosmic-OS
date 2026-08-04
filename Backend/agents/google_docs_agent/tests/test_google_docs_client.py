"""Tests for the Drive search query-building in GoogleDocsClient.list_documents."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from agents.google_docs_agent.google_docs_client import GoogleDocsClient  # noqa: E402


def _files_response(names: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"files": [{"id": f"file_{name}", "name": name} for name in names]},
    )


@pytest.mark.asyncio
async def test_list_documents_with_query_searches_by_name_and_sorts() -> None:
    """Reproduces a real production incident: every Drive search that
    combined a text query with sorting failed with a 403 ("Sorting is not
    supported for queries with fullText terms"), because the old query
    builder always OR'd `name contains` with `fullText contains` while also
    requesting orderBy. The orchestrator retried this same broken search 25
    times with different phrasing and never once succeeded, because the
    failure had nothing to do with phrasing. A title match should be tried
    first, since Drive can sort those and it covers the common "find the doc
    named X, most recently opened" case."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _files_response(["SPC Application - Fall 2026"])

    client = GoogleDocsClient("token-123")
    transport = httpx.MockTransport(handler)
    import agents.google_docs_agent.google_docs_client as module

    original_async_client = module.httpx.AsyncClient
    module.httpx.AsyncClient = lambda **_: original_async_client(transport=transport)  # type: ignore[assignment]
    try:
        matches = await client.list_documents(query="Fall 2026", max_results=10)
    finally:
        module.httpx.AsyncClient = original_async_client

    assert len(requests) == 1
    params = parse_qs(urlparse(str(requests[0].url)).query)
    assert "orderBy" in params
    assert "fullText" not in params["q"][0]
    assert "name contains 'Fall 2026'" in params["q"][0]
    assert [m["title"] for m in matches] == ["SPC Application - Fall 2026"]


@pytest.mark.asyncio
async def test_list_documents_falls_back_to_unsorted_fulltext_when_name_search_is_empty() -> None:
    """When nothing matches the title, fall back to a full-text content
    search - but without orderBy, since Drive rejects sorting combined with
    fullText. This preserves the old fullText-search capability instead of
    just dropping it, without ever reproducing the 403."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = parse_qs(urlparse(str(request.url)).query)
        if "fullText" in params["q"][0]:
            return _files_response(["Some doc mentioning the phrase"])
        return _files_response([])

    client = GoogleDocsClient("token-123")
    transport = httpx.MockTransport(handler)
    import agents.google_docs_agent.google_docs_client as module

    original_async_client = module.httpx.AsyncClient
    module.httpx.AsyncClient = lambda **_: original_async_client(transport=transport)  # type: ignore[assignment]
    try:
        matches = await client.list_documents(query="a phrase not in any title", max_results=10)
    finally:
        module.httpx.AsyncClient = original_async_client

    assert len(requests) == 2
    first_params = parse_qs(urlparse(str(requests[0].url)).query)
    assert "name contains" in first_params["q"][0]
    assert "orderBy" in first_params

    second_params = parse_qs(urlparse(str(requests[1].url)).query)
    assert "fullText contains" in second_params["q"][0]
    assert "orderBy" not in second_params
    assert [m["title"] for m in matches] == ["Some doc mentioning the phrase"]


@pytest.mark.asyncio
async def test_list_documents_without_query_sorts_by_recency_in_a_single_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _files_response(["Most recent doc"])

    client = GoogleDocsClient("token-123")
    transport = httpx.MockTransport(handler)
    import agents.google_docs_agent.google_docs_client as module

    original_async_client = module.httpx.AsyncClient
    module.httpx.AsyncClient = lambda **_: original_async_client(transport=transport)  # type: ignore[assignment]
    try:
        matches = await client.list_documents(query="", max_results=5)
    finally:
        module.httpx.AsyncClient = original_async_client

    assert len(requests) == 1
    params = parse_qs(urlparse(str(requests[0].url)).query)
    assert "orderBy" in params
    assert [m["title"] for m in matches] == ["Most recent doc"]
