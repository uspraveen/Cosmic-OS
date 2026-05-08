"""Tests for Pexels retry/fallback hardening in asset_manager.

These cover the production fix for transient Cloudflare 403 challenges that
previously killed entire deck builds. Net effect we exercise:
  - 403 + Cloudflare HTML body is retried, not surfaced.
  - resolve_photo() returns None on transient failure instead of raising.
"""
from __future__ import annotations

import httpx
import pytest

from agents.slide_agent import asset_manager as am


_CLOUDFLARE_HTML = (
    "<!DOCTYPE html>\n<html><head><title>Attention Required! | Cloudflare</title>"
    "</head><body>cf-ray cloudflare checking your browser</body></html>"
)


class _FakeResponse:
    def __init__(self, status_code: int, *, text: str = "", json_payload=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json = json_payload
        self.headers = headers or {}
        self.content = text.encode("utf-8") if text else b""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _install_fake_client(monkeypatch, responses):
    """Patch httpx.Client so each .request() call returns the next response."""
    queue = list(responses)

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, headers=None, params=None):
            if not queue:
                raise AssertionError("FakeClient: no more queued responses")
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr(am.httpx, "Client", _FakeClient)
    monkeypatch.setattr(am.time, "sleep", lambda *_a, **_k: None)
    return queue


def test_pexels_request_retries_cloudflare_then_succeeds(monkeypatch):
    queue = _install_fake_client(
        monkeypatch,
        [
            _FakeResponse(403, text=_CLOUDFLARE_HTML),
            _FakeResponse(200, text='{"photos": []}', json_payload={"photos": []}),
        ],
    )
    resp = am._pexels_request("GET", "https://api.pexels.com/v1/search", op="test.cf")
    assert resp.status_code == 200
    assert queue == []  # both responses consumed


def test_pexels_request_retries_5xx_then_succeeds(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _FakeResponse(503, text="upstream"),
            _FakeResponse(200, text="{}", json_payload={}),
        ],
    )
    resp = am._pexels_request("GET", "https://api.pexels.com/v1/search", op="test.5xx")
    assert resp.status_code == 200


def test_pexels_request_retries_network_error_then_succeeds(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            httpx.ConnectError("boom"),
            _FakeResponse(200, text="{}", json_payload={}),
        ],
    )
    resp = am._pexels_request("GET", "https://api.pexels.com/v1/search", op="test.net")
    assert resp.status_code == 200


def test_pexels_request_passes_through_non_transient_403(monkeypatch):
    # Real API-level 403 (JSON, not Cloudflare HTML) should NOT retry — surface it.
    _install_fake_client(
        monkeypatch,
        [_FakeResponse(403, text='{"error": "forbidden"}')],
    )
    resp = am._pexels_request("GET", "https://api.pexels.com/v1/search", op="test.api403")
    assert resp.status_code == 403


def test_pexels_request_exhausts_retries_raises_transient(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _FakeResponse(403, text=_CLOUDFLARE_HTML),
            _FakeResponse(503, text="<html>cloudflare</html>"),
            _FakeResponse(429, text="rate"),
        ],
    )
    with pytest.raises(am._PexelsTransientError):
        am._pexels_request("GET", "https://api.pexels.com/v1/search", op="test.exhaust")


def test_resolve_photo_returns_none_on_transient_search_failure(monkeypatch):
    monkeypatch.setattr(am, "PEXELS_API_KEY", "fake-key")

    def _boom(*_a, **_k):
        raise am._PexelsTransientError("simulated cloudflare")

    monkeypatch.setattr(am, "search_photos", _boom)
    assert am.resolve_photo("anything") is None


def test_resolve_photo_returns_none_when_search_raises_runtime(monkeypatch):
    monkeypatch.setattr(am, "PEXELS_API_KEY", "fake-key")

    def _boom(*_a, **_k):
        raise RuntimeError("Pexels API error 500: ...")

    monkeypatch.setattr(am, "search_photos", _boom)
    assert am.resolve_photo("anything") is None
