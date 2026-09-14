from __future__ import annotations

from shared.content_cards import (
    merge_content_cards_into_response_blocks,
    normalize_content_cards,
    project_specialist_content_cards,
)


def test_normalize_social_post_builds_sections_copy_and_fallback() -> None:
    result = normalize_content_cards(
        [
            {
                "preset": "social_post",
                "brand": "x",
                "title": "Draft 1",
                "body": "Ship the glass cards. #cosmic",
                "tags": ["#cosmic"],
                "mentions": ["@uspraveen"],
                "group_id": "x_drafts",
                "variant_index": 1,
                "variant_total": 2,
            }
        ]
    )

    assert result["status"] == "presented"
    assert result["card_count"] == 1
    card = result["response_blocks"][0]
    assert card["type"] == "content_card"
    assert card["preset"] == "social_post"
    assert card["brand"] == "x"
    assert card["character_limit"] == 280
    assert card["character_count"] == len("Ship the glass cards. #cosmic")
    assert card["group"] == {"id": "x_drafts", "index": 1, "total": 2}
    assert card["sections"][0]["type"] == "text"
    assert any(section["type"] == "chips" for section in card["sections"])
    assert card["actions"][0]["type"] == "copy"
    assert "Ship the glass cards" in card["fallback_text"]
    assert "id" in card and card["id"].startswith("content_card_")


def test_normalize_rejects_privileged_actions_and_unsafe_urls() -> None:
    result = normalize_content_cards(
        [
            {
                "title": "Launch note",
                "body": "Ready when you are.",
                "actions": [
                    {"type": "post"},
                    {"type": "send"},
                    {"type": "open_url", "url": "javascript:alert(1)"},
                    {"type": "open_url", "url": "http://example.com"},
                    {"type": "open_url", "url": "https://x.com/cosmic"},
                    {"type": "copy"},
                ],
            }
        ]
    )

    actions = {item["type"]: item for item in result["response_blocks"][0]["actions"]}
    assert "post" not in actions
    assert "send" not in actions
    assert actions["open_url"]["url"] == "https://x.com/cosmic"
    assert actions["copy"]["text"] == "Ready when you are."


def test_normalize_unknown_section_types_are_dropped_not_executed() -> None:
    result = normalize_content_cards(
        [
            {
                "title": "Visa packet",
                "sections": [
                    {"type": "html", "html": "<script>alert(1)</script>"},
                    {"type": "text", "text": "Bring your passport."},
                    {"type": "key_value", "rows": [{"label": "Stay", "value": "90 days"}]},
                ],
            }
        ]
    )
    types = [section["type"] for section in result["response_blocks"][0]["sections"]]
    assert types == ["text", "key_value"]


def test_normalize_rejects_empty_and_oversized_batches() -> None:
    assert normalize_content_cards([])["error"] is True
    assert normalize_content_cards([{"title": "x"} for _ in range(6)])["error"] is True


def test_project_x_search_results_uses_registered_view() -> None:
    projected = project_specialist_content_cards(
        intent="x.search",
        response={
            "notable_posts": [
                {
                    "author_handle": "foo",
                    "excerpt": "The glass cards landed.",
                    "why_it_matters": "Shows the current reaction.",
                    "post_url": "https://x.com/foo/status/1",
                    "posted_at": "2026-09-14",
                }
            ]
        },
        presentation={
            "view": "cosmic/x-search-results:v1",
            "data_path": "notable_posts",
        },
        channel="desktop",
    )
    assert projected is not None
    card = projected["response_blocks"][0]
    assert card["brand"] == "x"
    assert card["title"] == "@foo"
    assert any(action["type"] == "open_url" for action in card["actions"])


def test_project_x_search_results_skips_unsupported_channels_and_unknown_views() -> None:
    payload = {"notable_posts": [{"excerpt": "hello"}]}
    assert project_specialist_content_cards(
        intent="x.search",
        response=payload,
        presentation={"view": "cosmic/x-search-results:v1", "data_path": "notable_posts"},
        channel="whatsapp",
    ) is None
    assert project_specialist_content_cards(
        intent="x.search",
        response=payload,
        presentation={"view": "cosmic/invented:v1", "data_path": "notable_posts"},
        channel="desktop",
    ) is None


def test_merge_content_cards_builds_markdown_when_needed_and_dedupes() -> None:
    card = normalize_content_cards([{"title": "Draft", "body": "Hello"}])["response_blocks"][0]
    merged = merge_content_cards_into_response_blocks(None, [card], display_text="Here are drafts.")
    assert merged is not None
    assert merged[0]["type"] == "markdown"
    assert merged[-1]["id"] == card["id"]
    again = merge_content_cards_into_response_blocks(merged, [card])
    assert [item["id"] for item in again or []].count(card["id"]) == 1
