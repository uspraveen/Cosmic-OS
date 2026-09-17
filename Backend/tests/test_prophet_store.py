from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gateway.prophet import ProphetStore, ProphetValidationError
from gateway.prophet.store import prophet_catchup_since_date


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


def test_update_settings_persists_full_desktop_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.update_settings(
        {
            "enabled": True,
            "morning_time": "06:45",
            "evening_enabled": False,
            "evening_time": "20:30",
            "max_stories": 18,
            "notifications_enabled": False,
            "paper_style": "midnight",
            "sections": [
                {"id": "tech", "label": "Technology", "enabled": True},
                {"id": "markets", "label": "Markets", "enabled": False},
            ],
        }
    )
    assert saved["paper_style"] == "midnight"
    assert saved["notifications_enabled"] is False
    assert saved["sections"] == [
        {"id": "tech", "label": "Technology", "enabled": True},
        {"id": "markets", "label": "Markets", "enabled": False},
    ]
    reloaded = store.get_settings()
    assert reloaded["morning_time"] == "06:45"
    assert reloaded["evening_enabled"] is False
    assert reloaded["sections"][1]["enabled"] is False


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


def test_lead_only_edition_is_published(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = {
        "slot": "morning",
        "sections": [
            {
                "id": "breaking",
                "label": "Breaking",
                "stories": [{"headline": "Only story of the day", "role": "lead", "importance": 90}],
            }
        ],
    }
    result = store.publish_edition(payload)
    edition = result["edition"]
    assert result["story_count"] == 1
    assert edition["lead"]["headline"] == "Only story of the day"
    assert edition["sections"] == []


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


def test_story_index_migrates_embedding_columns(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy-stories.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
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
        );
        CREATE TABLE prophet_story_index (
            story_row_id TEXT PRIMARY KEY,
            edition_id TEXT NOT NULL,
            edition_date TEXT NOT NULL,
            slot TEXT NOT NULL,
            story_id TEXT,
            url_hash TEXT,
            headline TEXT NOT NULL,
            headline_key TEXT NOT NULL,
            section_id TEXT,
            shown_at TEXT NOT NULL
        );
        INSERT INTO prophet_settings (config_id, updated_at)
        VALUES ('default', '2026-01-01T00:00:00Z');
        """
    )
    connection.commit()
    connection.close()

    store = ProphetStore(db_path)
    store.initialize()
    result = store.publish_edition(_edition())
    rows = store.list_story_index_for_edition(result["edition_id"])
    assert rows
    assert "dek" in rows[0]
    store.update_story_embedding(
        rows[0]["story_row_id"],
        embedding_model="pplx-embed-v1-4b",
        embedding_dimensions=2,
        embedding_vector=[1.0, 0.0],
    )
    reloaded = store.list_story_index_for_edition(result["edition_id"])
    assert reloaded[0]["embedding_vector"] is not None
    assert abs(reloaded[0]["embedding_vector"][0]) > 0.9


def test_similar_stories_ranks_by_cosine_and_respects_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.publish_edition(_edition())
    evening = _edition()
    evening["slot"] = "evening"
    evening["lead"]["headline"] = "Completely unrelated markets note"
    evening["lead"]["source"] = {"name": "FT", "url": "https://example.com/markets"}
    evening["sections"][0]["stories"] = [
        {
            "headline": "Bond yields jumped",
            "source": {"name": "FT", "url": "https://example.com/bonds"},
        }
    ]
    second = store.publish_edition(evening)

    agent_rows = store.list_story_index_for_edition(first["edition_id"])
    market_rows = store.list_story_index_for_edition(second["edition_id"])
    for row in agent_rows:
        store.update_story_embedding(
            row["story_row_id"],
            embedding_model="test",
            embedding_dimensions=2,
            embedding_vector=[1.0, 0.0],
        )
    for row in market_rows:
        store.update_story_embedding(
            row["story_row_id"],
            embedding_model="test",
            embedding_dimensions=2,
            embedding_vector=[0.0, 1.0],
        )

    matches = store.similar_stories(
        [[1.0, 0.0]],
        days=28,
        exclude_edition_id=first["edition_id"],
        min_similarity=0.7,
        limit=8,
    )
    assert matches == []

    matches = store.similar_stories(
        [[1.0, 0.0]],
        days=28,
        exclude_edition_id=second["edition_id"],
        min_similarity=0.7,
        limit=8,
    )
    assert matches
    assert all("agent" in str(item["headline"]).lower() or "tech" in str(item["headline"]).lower()
               or item["edition_id"] == first["edition_id"] for item in matches)
    assert matches[0]["semantic_similarity"] >= 0.99



def test_notification_lifecycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = store.publish_edition(_edition())
    edition_id = result['edition_id']

    note = store.create_notification_for_edition(
        edition_id=edition_id,
        edition_date='2026-09-11',
        slot='morning',
        headline='Lead story about agents',
        story_count=5,
    )
    assert note['state'] == 'pending'
    assert note['edition_id'] == edition_id

    pending = store.list_pending_notifications(edition_date='2026-09-11')
    assert len(pending) == 1
    assert pending[0]['notification_id'] == note['notification_id']

    delivered = store.mark_notification_state(note['notification_id'], state='delivered', reason='test')
    assert delivered is not None
    assert delivered['state'] == 'delivered'
    assert delivered['delivered_at'] is not None

    # Still surfaces until opened or ignored
    pending = store.list_pending_notifications(edition_date='2026-09-11')
    assert len(pending) == 1

    opened = store.mark_notification_state(note['notification_id'], state='opened', reason='test')
    assert opened is not None
    assert opened['state'] == 'opened'
    assert opened['opened_at'] is not None

    # Terminal: no longer pending
    pending = store.list_pending_notifications(edition_date='2026-09-11')
    assert pending == []

    # Terminal states win - cannot move back
    again = store.mark_notification_state(note['notification_id'], state='delivered', reason='test')
    assert again is not None
    assert again['state'] == 'opened'


def test_prophet_catchup_since_date_uses_user_timezone_not_utc() -> None:
    # 01:00 UTC on Sep 17 is still Sep 16 evening in Chicago.
    now = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)
    assert prophet_catchup_since_date("America/Chicago", now=now) == "2026-09-15"
    assert prophet_catchup_since_date("UTC", now=now) == "2026-09-16"


def test_list_pending_notifications_since_date_keeps_yesterday(tmp_path: Path) -> None:
    store = _store(tmp_path)
    morning = store.publish_edition(_edition())
    evening_payload = _edition()
    evening_payload["edition_date"] = "2026-09-10"
    evening_payload["slot"] = "evening"
    evening_payload["lead"]["headline"] = "Yesterday evening wrap"
    evening = store.publish_edition(evening_payload, slot="evening")

    store.create_notification_for_edition(
        edition_id=morning["edition_id"],
        edition_date="2026-09-11",
        slot="morning",
        headline="Today morning",
        story_count=5,
    )
    store.create_notification_for_edition(
        edition_id=evening["edition_id"],
        edition_date="2026-09-10",
        slot="evening",
        headline="Yesterday evening wrap",
        story_count=8,
    )

    pending = store.list_pending_notifications(since_date="2026-09-10", limit=4)
    dates = {item["edition_date"] for item in pending}
    assert dates == {"2026-09-11", "2026-09-10"}

    today_only = store.list_pending_notifications(edition_date="2026-09-11")
    assert {item["edition_date"] for item in today_only} == {"2026-09-11"}


def test_notification_snooze_expires(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone
    store = _store(tmp_path)
    result = store.publish_edition(_edition())

    note = store.create_notification_for_edition(
        edition_id=result['edition_id'],
        edition_date='2026-09-11',
        slot='morning',
        headline='Lead',
        story_count=5,
    )
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace('+00:00', 'Z')
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat().replace('+00:00', 'Z')

    snoozed = store.mark_notification_state(
        note['notification_id'], state='snoozed', snoozed_until=future, reason='later',
    )
    assert snoozed is not None
    assert snoozed['state'] == 'snoozed'
    assert store.list_pending_notifications(edition_date='2026-09-11') == []

    # Expired snooze resurfaces
    snoozed = store.mark_notification_state(
        note['notification_id'], state='snoozed', snoozed_until=past, reason='later',
    )
    assert snoozed is not None
    pending = store.list_pending_notifications(edition_date='2026-09-11')
    assert len(pending) == 1
    assert pending[0]['state'] == 'snoozed'


def test_notification_upsert_on_republish(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.publish_edition(_edition())
    second = store.publish_edition(_edition())
    assert first['edition_id'] == second['edition_id']

    note = store.create_notification_for_edition(
        edition_id=first['edition_id'],
        edition_date='2026-09-11',
        slot='morning',
        headline='Original',
        story_count=5,
    )
    note2 = store.create_notification_for_edition(
        edition_id=first['edition_id'],
        edition_date='2026-09-11',
        slot='morning',
        headline='Updated headline',
        story_count=6,
    )
    assert note['notification_id'] == note2['notification_id']
    assert note2['headline'] == 'Updated headline'


def test_publish_slot_kwarg_overrides_payload_and_keeps_editions_apart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    morning = store.publish_edition(_edition(), slot="morning")
    assert morning["slot"] == "morning"

    evening_payload = _edition()
    evening_payload["slot"] = "morning"
    evening_payload["lead"]["headline"] = "Evening wrap about agents"
    evening = store.publish_edition(evening_payload, slot="evening")
    assert evening["slot"] == "evening"
    assert evening["edition_id"] != morning["edition_id"]
    assert store.get_edition("2026-09-11", "morning")["payload"]["lead"]["headline"] == "Lead story about agents"
    assert store.get_edition("2026-09-11", "evening")["payload"]["lead"]["headline"] == "Evening wrap about agents"


def test_prophet_slot_from_source_id() -> None:
    from gateway.prophet import (
        PROPHET_EVENING_CRON_ID,
        PROPHET_MORNING_CRON_ID,
        prophet_slot_from_source_id,
    )

    assert prophet_slot_from_source_id(PROPHET_EVENING_CRON_ID) == "evening"
    assert prophet_slot_from_source_id(PROPHET_MORNING_CRON_ID) == "morning"
    assert prophet_slot_from_source_id("cron_todo") is None
    assert prophet_slot_from_source_id(None) is None
