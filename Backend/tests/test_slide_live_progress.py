"""Live slide-build snapshots must stay compact and accumulate per-slide PNGs."""
from __future__ import annotations

from unittest.mock import MagicMock

from agents.slide_agent.live_progress import LiveDeckProgress, compact_slide_plan
from gateway.runtime import GatewayRuntime


def test_compact_slide_plan_drops_full_content():
    compact = compact_slide_plan(
        {
            "deck_title": "Seed round",
            "deck_theme": "midnight",
            "slides": [
                {
                    "slide_number": 1,
                    "title": "Cover",
                    "content_role": "title",
                    "full_content": "x" * 5000,
                }
            ],
        }
    )
    assert compact["deck_title"] == "Seed round"
    assert compact["slides"][0]["title"] == "Cover"
    assert compact["slides"][0]["content_role"] == "title"
    assert "full_content" not in compact["slides"][0]


def test_live_deck_progress_accumulates_slides_and_plan():
    tracker = LiveDeckProgress()
    tracker.apply(
        {
            "stage": "plan",
            "label": "Planned a 2-slide deck",
            "plan": {
                "deck_title": "Hardware founders",
                "slides": [
                    {"slide_number": 1, "title": "Cover", "content_role": "title"},
                    {"slide_number": 2, "title": "Team", "content_role": "body"},
                ],
            },
            "total": 2,
        }
    )
    tracker.apply(
        {
            "stage": "qa",
            "label": "QA slide 1",
            "slide_number": 1,
            "title": "Cover",
            "png_path": "/tmp/slide-01.png",
            "status": "qa",
        }
    )
    snapshot = tracker.apply(
        {
            "stage": "qa",
            "label": "QA slide 2",
            "slide_number": 2,
            "title": "Team",
            "png_path": "/tmp/slide-02.png",
        }
    )
    assert snapshot["kind"] == "slide_build"
    assert snapshot["plan"]["deck_title"] == "Hardware founders"
    assert [item["slide_number"] for item in snapshot["slides"]] == [1, 2]
    assert snapshot["slides"][0]["png_path"].endswith("slide-01.png")
    assert snapshot["current"] == 2
    assert snapshot["total"] == 2


def test_absorb_slides_keeps_stamped_artifact_ids():
    tracker = LiveDeckProgress()
    tracker.apply(
        {
            "stage": "render",
            "label": "Rendered slide 1",
            "slide_number": 1,
            "png_path": "/tmp/raw-slide-01.png",
        }
    )
    tracker.absorb_slides(
        [
            {
                "slide_number": 1,
                "path": "runs/artifacts/tsk/previews/slide-01.png",
                "artifact_id": "art_preview_1",
                "filename": "slide-01.png",
            }
        ]
    )
    snapshot = tracker.snapshot()
    assert snapshot["slides"][0]["artifact_id"] == "art_preview_1"
    assert snapshot["slides"][0]["filename"] == "slide-01.png"


def test_hydrate_slide_progress_mints_preview_urls_without_breaking_progress_persist_guard():
    rt = GatewayRuntime.__new__(GatewayRuntime)
    rt._cache_artifact_list = MagicMock()
    rt._normalize_produced_artifact_list = lambda artifacts: artifacts  # type: ignore[method-assign]
    rt.mint_artifact_access_url = MagicMock(return_value="cosmic://preview/slide-01.png")  # type: ignore[method-assign]

    hydrated = rt._hydrate_slide_progress(
        {
            "kind": "slide_build",
            "stage": "qa",
            "label": "QA slide 1",
            "current": 1,
            "total": 2,
            "slides": [
                {
                    "slide_number": 1,
                    "title": "Cover",
                    "artifact_id": "art_preview_1",
                    "path": "runs/artifacts/tsk/previews/slide-01.png",
                    "mime": "image/png",
                    "filename": "slide-01.png",
                    "sha256": "abc",
                }
            ],
        },
        request_id="req_1",
        session_id="sess_1",
        channel="desktop",
    )

    assert hydrated["slides"][0]["preview_url"] == "cosmic://preview/slide-01.png"
    rt._cache_artifact_list.assert_called_once()
    persisted = rt._cache_artifact_list.call_args.kwargs["artifacts"]
    assert persisted[0]["artifact_id"] == "art_preview_1"
    assert persisted[0]["kind"] == "output"


def test_specialist_activity_metadata_copies_preview_url():
    rt = GatewayRuntime.__new__(GatewayRuntime)
    metadata = rt._extract_activity_specialist_metadata(
        {
            "specialist": {
                "task_id": "tsk_slide",
                "attach_to_task_id": "tsk_parent",
                "agent_id": "slide_agent",
                "agent_label": "Slide Agent",
                "intent": "slide.create",
                "event_type": "task.progress",
            },
            "slide_progress": {
                "current": 2,
                "slides": [
                    {
                        "slide_number": 1,
                        "preview_url": "cosmic://preview/slide-01.png",
                    },
                    {
                        "slide_number": 2,
                        "preview_url": "cosmic://preview/slide-02.png",
                    },
                ],
            },
        }
    )
    assert metadata["preview_url"] == "cosmic://preview/slide-02.png"
    assert metadata["slide_number"] == 2
    assert metadata["agent_id"] == "slide_agent"
