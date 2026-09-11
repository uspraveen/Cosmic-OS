from __future__ import annotations

from pathlib import Path

import pytest

from gateway.prophet import ProphetStore, ProphetValidationError


def _store(tmp_path: Path) -> ProphetStore:
    store = ProphetStore(tmp_path / "prophet.db")
    store.initialize()
    return store


def _edition() -> dict:
    return {
        "edition_date": "2026-09-11",
        "slot": "morning",
        "editor_note": "Test edition",
        "lead": {
            "headline": "Lead story about agents",
            "dek": "A dek",
            "body": ["First paragraph.", "Second paragraph."],
            "importance": 95,
            "role": "lead",
            "source": {"name": "Reuters", "url": "https://example.com/lead"},
            "image": {"url": "https://example.com/lead.jpg", "caption": "Art"},
        },
        "sections": [
            {
                "id": "tech",
                "label": "Technology",
                "layout": "feature",
                "stories": [
                    {
                        "headline": "Tech story one",
                        "body": ["Body."],
                        "importance": 80,
                        "source": {"name": "The Verge", "url": "https://example.com/tech-1"},
                    },
                    {
                        "headline": "Tech story two",
                        "role": "brief",
                        "body": ["This body should be trimmed."],
                        "importance": 60,
                        "source": {"name": "Wired", "url": "https://example.com/tech-2"},
                    },
                ],
            }
        ],
    }


def test_settings_defaults(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = store.get_settings()
    assert settings["enabled"] is True
    assert settings["morning_time"] == "05:00"
    assert settings["evening_enabled"] is True
    assert settings["evening_time"] == "19:00"
    assert settings["max_stories"] == 15
    assert settings["paper_style"] == "parchment"
    assert len(settings["sections"]) == 5


def test_update_settings_validates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    updated = store.update_settings({"morning_time": "06:30", "max_stories": 20})
    assert updated["morning_time"] == "06:30"
    assert updated["max_stories"] == 20
    styled = store.update_settings({"paper_style": "newsprint"})
    assert styled["paper_style"] == "newsprint"
    with pytest.raises(ProphetValidationError):
        store.update_settings({"morning_time": "6:30"})
    with pytest.raises(ProphetValidationError):
        store.update_settings({"max_stories": 31})
    with pytest.raises(ProphetValidationError):
        store.update_settings({"paper_style": "cardboard"})


def test_initialize_migrates_paper_style_column(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE prophet_settings (
            config_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            morning_time TEXT NOT NULL DEFAULT '05:00',
            evening_enabled INTEGER NOT NULL DEFAULT 1,
            evening_time TEXT NOT NULL DEFAULT '19:00',
            max_stories INTEGER NOT NULL DEFAULT 15,
            notifications_enabled INTEGER NOT NULL DEFAULT 1,
            sections_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO prophet_settings (config_id, updated_at) VALUES ('default', '2026-01-01T00:00:00Z')"
    )
    connection.commit()
    connection.close()

    store = ProphetStore(db_path)
    store.initialize()
    settings = store.get_settings()
    assert settings["paper_style"] == "parchment"


def test_publish_and_fetch_edition(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.publish_edition(_edition(), request_id="req_cron_test")
    assert result["story_count"] == 3
    assert result["slot"] == "morning"
    warnings = result["warnings"]
    assert any("trimmed to a one-line brief" in warning for warning in warnings)

    fetched = store.get_edition("2026-09-11", "morning")
    assert fetched is not None
    assert fetched["payload"]["lead"]["headline"] == "Lead story about agents"
    assert fetched["revision"] == 1

    by_request = store.find_edition_by_request_id("req_cron_test")
    assert by_request is not None
    assert by_request["edition_id"] == result["edition_id"]


def test_republish_increments_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.publish_edition(_edition())
    second = store.publish_edition(_edition())
    assert second["revision"] == first["revision"] + 1
    assert second["edition_id"] == first["edition_id"]
    assert not any("appeared in a recent edition" in warning for warning in second["warnings"])


def test_dedup_warns_on_repeat(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish_edition(_edition())
    repeat = _edition()
    repeat["slot"] = "evening"
    result = store.publish_edition(repeat)
    assert any("appeared in a recent edition" in warning for warning in result["warnings"])


def test_story_cap_trims_lowest_importance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.update_settings({"max_stories": 5})
    payload = _edition()
    payload["sections"][0]["stories"] = [
        {
            "headline": f"Story {index}",
            "importance": 70 - index,
            "source": {"name": "Wire", "url": f"https://example.com/{index}"},
        }
        for index in range(8)
    ]
    result = store.publish_edition(payload)
    assert result["story_count"] == 5
    assert any("Trimmed" in warning for warning in result["warnings"])


def test_placeholder_editions_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _edition()
    payload.pop("lead")
    payload["sections"] = [
        {
            "id": "test",
            "label": "Test",
            "stories": [
                {"headline": "Test story one"},
                {"headline": "Test story two"},
                {"headline": "Test story three"},
            ],
        }
    ]
    with pytest.raises(ProphetValidationError) as excinfo:
        store.publish_edition(payload)
    assert excinfo.value.code == "placeholder_edition"


def test_lead_auto_promoted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _edition()
    payload.pop("lead")
    payload["sections"][0]["stories"][0]["role"] = "lead"
    result = store.publish_edition(payload)
    edition = result["edition"]
    assert edition["lead"]["headline"] == "Tech story one"
    assert result["story_count"] == 2


def test_inferred_interests_removable_user_protected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_interest("AI agents", origin="inferred")
    store.upsert_interest("Chip design", origin="user")
    assert store.remove_interest("AI agents") is True
    assert store.remove_interest("Chip design") is False
    topics = {item["topic"] for item in store.list_interests()}
    assert topics == {"Chip design"}


def test_sources_upsert_and_remove(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = store.upsert_source(kind="x", value="@anthropic", label="Anthropic")
    assert source is not None
    assert len(store.list_sources()) == 1
    assert store.remove_source(source["source_id"]) is True
    assert store.list_sources() == []


def test_list_editions_summary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.publish_edition(_edition())
    editions = store.list_editions(limit=5)
    assert len(editions) == 1
    assert editions[0]["headlines"][0]["headline"] == "Lead story about agents"


def test_image_cap_keeps_lead_and_top_stories(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _edition()
    payload["sections"][0]["stories"] = [
        {
            "headline": f"Visual {index}",
            "importance": 80 - index,
            "image": {"url": f"https://example.com/photo-{index}.jpg"},
        }
        for index in range(8)
    ]
    result = store.publish_edition(payload)
    edition = result["edition"]
    stories = ([edition["lead"]] if edition.get("lead") else []) + [
        story for section in edition["sections"] for story in section["stories"]
    ]
    with_images = [
        story
        for story in stories
        if isinstance(story.get("image"), dict) and story["image"].get("url")
    ]
    assert len(with_images) <= 4
    assert edition["lead"]["image"]["url"].endswith("lead.jpg")
    assert any("Trimmed an extra image" in warning for warning in result["warnings"])


def test_weak_and_duplicate_images_are_dropped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _edition()
    payload["lead"]["image"] = {"url": "https://example.com/logo.png"}
    stories = payload["sections"][0]["stories"]
    stories[0]["image"] = {"url": "https://example.com/icon.svg"}
    stories[1]["image"] = {"url": "https://example.com/shared-photo.jpg"}
    payload["sections"].append(
        {
            "id": "extra",
            "label": "Extra",
            "stories": [
                {
                    "headline": "Duplicate visual",
                    "image": {"url": "https://example.com/shared-photo.jpg"},
                }
            ],
        }
    )
    result = store.publish_edition(payload)
    edition = result["edition"]
    assert "image" not in edition["lead"]
    assert "image" not in edition["sections"][0]["stories"][0]
    extra = next(section for section in edition["sections"] if section["id"] == "extra")
    assert "image" not in extra["stories"][0]
    assert any("duplicate image" in warning for warning in result["warnings"])
