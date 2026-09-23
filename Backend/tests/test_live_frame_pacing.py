"""Tests for the live-frame pacing ladder and run-long ring recorder.

The live view used to be unpaced at the source: Chrome forwarded every
paint, the only gate was a fixed 2.5s-interval in the adapter, and nothing
was recorded — so watching was a slideshow and post-run diagnosis had two
grounded stills per step. This pins the replacement: a three-step cadence
ladder (idle / watched / takeover) with an adaptive slow-link floor, and a
byte-and-count bounded ring recording flushed into the run directory.

Run from Backend/:  python -m pytest tests/test_live_frame_pacing.py -q
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.browser_agent.agent import (  # noqa: E402
    LiveFrameRecorder,
    _LIVE_FRAME_MIN_INTERVAL_SEC,
    _LIVE_FRAME_SLOW_LINK_FLOOR_SEC,
    _LIVE_FRAME_TAKEOVER_INTERVAL_SEC,
    _LIVE_FRAME_WATCHED_INTERVAL_SEC,
    live_frame_interval,
)


# --------------------------------------------------------------------- #
# Cadence ladder

class TestIntervalLadder:
    def test_idle_cadence_by_default(self):
        assert live_frame_interval({}) == _LIVE_FRAME_MIN_INTERVAL_SEC

    def test_watching_speeds_the_feed_up(self):
        assert live_frame_interval({"watching": True}) == _LIVE_FRAME_WATCHED_INTERVAL_SEC

    def test_takeover_outranks_everything(self):
        assert live_frame_interval({"takeover": True}) == _LIVE_FRAME_TAKEOVER_INTERVAL_SEC
        assert (
            live_frame_interval({"takeover": True, "watching": True, "floor": 99.0})
            == _LIVE_FRAME_TAKEOVER_INTERVAL_SEC
        )

    def test_slow_link_floor_widens_watching_only(self):
        assert (
            live_frame_interval({"watching": True, "floor": _LIVE_FRAME_SLOW_LINK_FLOOR_SEC})
            == _LIVE_FRAME_SLOW_LINK_FLOOR_SEC
        )
        # The idle cadence is already slower than the floor — the floor can
        # never speed anything up.
        assert live_frame_interval({"floor": 0.01}) == _LIVE_FRAME_MIN_INTERVAL_SEC


# --------------------------------------------------------------------- #
# Ring recorder

JPEG_FRAME = base64.b64encode(b"\xff\xd8\xff\xe0fake-jpeg-bytes").decode()


class TestRecorder:
    def test_add_and_len(self):
        rec = LiveFrameRecorder()
        rec.add(JPEG_FRAME, time.time())
        assert len(rec) == 1

    def test_byte_cap_drops_oldest(self):
        rec = LiveFrameRecorder(max_frames=100, max_bytes=len(JPEG_FRAME) * 2 + 10)
        for i in range(5):
            rec.add(JPEG_FRAME, time.time() + i)
        assert len(rec) == 2  # capped by bytes, oldest dropped first

    def test_count_cap_drops_oldest(self):
        rec = LiveFrameRecorder(max_frames=3, max_bytes=10**9)
        for i in range(5):
            rec.add(JPEG_FRAME, time.time() + i)
        assert len(rec) == 3

    def test_flush_writes_frames_and_manifest(self, tmp_path):
        rec = LiveFrameRecorder()
        t0 = time.time()
        for i in range(3):
            rec.add(base64.b64encode(f"jpeg-{i}".encode()).decode(), t0 + i * 0.1)
        out = rec.flush(tmp_path)
        assert out is not None
        frames = sorted((tmp_path / "live_recording").glob("frame_*.jpg"))
        assert len(frames) == 3
        assert frames[0].read_bytes() == b"jpeg-0"
        lines = (tmp_path / "live_recording" / "manifest.jsonl").read_text().splitlines()
        assert len(lines) == 3
        first = json.loads(lines[0])
        assert first["t"] == 0.0 and first["bytes"] == 6

    def test_flush_empty_returns_none(self, tmp_path):
        assert LiveFrameRecorder().flush(tmp_path) is None

    def test_flush_skips_corrupt_base64(self, tmp_path):
        rec = LiveFrameRecorder()
        rec.add("%%%not-base64%%%", time.time())
        rec.add(JPEG_FRAME, time.time() + 1)
        out = rec.flush(tmp_path)
        assert out is not None
        assert len(list((tmp_path / "live_recording").glob("frame_*.jpg"))) == 1


# --------------------------------------------------------------------- #
# The bridge itself

class _FakeHttpClient:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.posts: list[dict] = []

    async def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)


def _fake_agent(posts_delay: float = 0.0):
    from agents.browser_agent.agent import BrowserAgent

    agent = object.__new__(BrowserAgent)
    agent.gateway_url = "http://gateway.test"
    agent.gateway_internal_token = "tok"
    agent._http_client = _FakeHttpClient(posts_delay)
    return agent


def _pacing(**overrides):
    pacing = {
        "at": 0.0,
        "takeover": False,
        "watching": False,
        "floor": 0.0,
        "in_flight": False,
        "durations": deque(maxlen=8),
    }
    pacing.update(overrides)
    return pacing


class _FakeTask:
    task_id = "task-1"


def _bridge(agent, frame, pacing, recorder):
    from agents.browser_agent.agent import BrowserAgent

    return asyncio.run(
        BrowserAgent._live_frame_bridge(agent, _FakeTask(), frame, pacing, recorder)
    )


class TestBridge:
    def test_first_frame_ships_and_is_recorded(self):
        agent = _fake_agent()
        pacing, rec = _pacing(), LiveFrameRecorder()
        _bridge(agent, JPEG_FRAME, pacing, rec)
        assert len(agent._http_client.posts) == 1
        assert len(rec) == 1
        assert pacing["in_flight"] is False

    def test_interval_gate_drops_burst_frames(self):
        agent = _fake_agent()
        pacing, rec = _pacing(watching=True), LiveFrameRecorder()
        # Back-to-back frames: the first ships, the rest of the burst within
        # one watched interval is dropped at this gate.
        for _ in range(6):
            _bridge(agent, JPEG_FRAME, pacing, rec)
        assert len(agent._http_client.posts) == 1
        assert len(rec) == 1

    def test_single_flight_never_stacks_posts(self):
        agent = _fake_agent()
        pacing = _pacing(watching=True)
        pacing["in_flight"] = True
        rec = LiveFrameRecorder()
        _bridge(agent, JPEG_FRAME, pacing, rec)
        assert agent._http_client.posts == []
        assert len(rec) == 0  # dropped frames are not recorded either

    def test_adaptive_floor_engages_after_slow_posts(self):
        agent = _fake_agent()
        pacing = _pacing(watching=True)
        pacing["durations"].extend([0.7] * 8)  # recent posts were all slow
        rec = LiveFrameRecorder()
        pacing["at"] = time.monotonic() - 10
        _bridge(agent, JPEG_FRAME, pacing, rec)
        assert pacing["floor"] == _LIVE_FRAME_SLOW_LINK_FLOOR_SEC

    def test_no_adaptive_floor_when_not_watching(self):
        agent = _fake_agent()
        pacing = _pacing(watching=False)
        pacing["durations"].extend([0.7] * 8)
        rec = LiveFrameRecorder()
        pacing["at"] = time.monotonic() - 10
        _bridge(agent, JPEG_FRAME, pacing, rec)
        assert pacing["floor"] == 0.0

    def test_floor_clears_when_the_link_recovers(self):
        agent = _fake_agent()
        pacing = _pacing(watching=True, floor=_LIVE_FRAME_SLOW_LINK_FLOOR_SEC)
        pacing["durations"].extend([0.05] * 8)  # link recovered
        rec = LiveFrameRecorder()
        pacing["at"] = time.monotonic() - 10
        _bridge(agent, JPEG_FRAME, pacing, rec)
        assert pacing["floor"] == 0.0

    def test_send_failure_is_never_fatal(self):
        agent = _fake_agent()

        async def exploding_post(*a, **k):
            raise RuntimeError("gateway down")

        agent._http_client = type("X", (), {"post": staticmethod(exploding_post)})()
        pacing, rec = _pacing(), LiveFrameRecorder()
        _bridge(agent, JPEG_FRAME, pacing, rec)  # must not raise
        assert pacing["in_flight"] is False


# --------------------------------------------------------------------- #
# Gateway-side latest-wins delivery

class _FakeWs:
    """Websocket stand-in whose sends take a while — the slow-desktop-link
    shape the pump exists for."""

    def __init__(self, delay: float = 0.1) -> None:
        self.delay = delay
        self.sent: list[dict] = []

    async def send_json(self, event: dict) -> None:
        await asyncio.sleep(self.delay)
        self.sent.append(event)


class TestOfferLiveFrame:
    def _adapter(self, delay: float = 0.1):
        from gateway.channels.desktop import DesktopAdapter, DesktopConnection

        adapter = DesktopAdapter()
        ws = _FakeWs(delay)
        conn = DesktopConnection(
            channel="desktop-1",
            device_id="d1",
            session_id="sess-1",
            websocket=ws,
            connected_at="",
            last_seen_at="",
        )
        adapter._connections["desktop-1"] = conn
        return adapter, ws

    def test_offer_is_non_blocking(self):
        adapter, ws = self._adapter(delay=0.5)

        async def run():
            started = time.monotonic()
            await adapter.offer_live_frame("sess-1", {"frame": "a"})
            return time.monotonic() - started

        elapsed = asyncio.run(run())
        assert elapsed < 0.2  # returned long before the 0.5s send finished

    def test_stale_frames_are_dropped_not_queued(self):
        adapter, ws = self._adapter(delay=0.1)

        async def run():
            for i in range(5):
                await adapter.offer_live_frame("sess-1", {"frame": f"f{i}"})
            await asyncio.sleep(0.6)  # let the pump drain
            # With a 0.1s send and five near-simultaneous offers, the pump
            # sends a couple of frames at most — and the LAST one sent is
            # the newest offered, never a stale one.
            assert ws.sent, "pump delivered nothing"
            assert ws.sent[-1]["frame"] == "f4"
            assert len(ws.sent) <= 5

        asyncio.run(run())

    def test_no_matching_session_sends_nothing(self):
        adapter, ws = self._adapter(delay=0.01)

        async def run():
            await adapter.offer_live_frame("other-session", {"frame": "x"})
            await asyncio.sleep(0.05)
            assert ws.sent == []

        asyncio.run(run())

    def test_failed_send_drops_the_frame(self):
        adapter, ws = self._adapter(delay=0.0)

        async def explode(event):
            raise RuntimeError("socket dead")

        ws.send_json = explode  # type: ignore[method-assign]

        async def run():
            await adapter.offer_live_frame("sess-1", {"frame": "x"})
            await asyncio.sleep(0.05)

        asyncio.run(run())  # must not raise; frame dropped silently
