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


def test_checklist_items_survive_long_instructions() -> None:
    long_line = "Demo: 60-90s clip of intent-to-Gerbers, or a screenshot set if video is too much friction this week"
    result = normalize_content_cards(
        [
            {
                "preset": "checklist",
                "title": "Coppr update skeleton",
                "items": [long_line, "Traction: one number that moved"],
            }
        ]
    )

    assert result["status"] == "presented"
    section = result["response_blocks"][0]["sections"][0]
    assert section["style"] == "checklist"
    assert section["items"][0] == long_line
    assert not section["items"][0].endswith("…")


def test_chip_items_keep_the_tight_cap() -> None:
    chip = "#" + "x" * 200
    result = normalize_content_cards([{"title": "Draft", "body": "Body", "tags": [chip]}])

    card = result["response_blocks"][0]
    assert card["tags"][0].endswith("…")
    assert len(card["tags"][0]) <= 80


def test_stat_metrics_note_sections_normalize_and_land_in_fallback() -> None:
    result = normalize_content_cards(
        [
            {
                "title": "Shadeform reported results",
                "sections": [
                    {
                        "type": "metrics",
                        "stats": [
                            {"label": "Initial decode", "value": "7.2 tok/s"},
                            {"label": "Optimized decode", "value": "191 tok/s", "highlight": True},
                        ],
                    },
                    {"type": "note", "text": "Reported measurements, not independently reproduced."},
                    {"type": "stat", "label": "Total GPU memory", "value": "192 GB", "qualifier": "4 x 48 GB"},
                    {"type": "stat", "label": "Missing figure"},
                ],
                "actions": [
                    {"type": "open_url", "url": "https://shadeform.ai/report", "label": "Read the report"}
                ],
            }
        ]
    )

    assert result["status"] == "presented"
    card = result["response_blocks"][0]
    types = [section["type"] for section in card["sections"]]
    assert types == ["metrics", "note", "stat"]
    metrics = card["sections"][0]
    assert metrics["stats"][1] == {"label": "Optimized decode", "value": "191 tok/s", "highlight": True}
    assert "figures" in result["covers"]
    assert "caveats" in result["covers"]
    fallback = card["fallback_text"]
    assert "Initial decode: 7.2 tok/s" in fallback
    assert "Optimized decode: 191 tok/s" in fallback
    assert "Total GPU memory: 192 GB" in fallback
    assert "4 x 48 GB" in fallback
    assert any(action["type"] == "open_url" for action in card["actions"])


def test_flow_section_requires_two_nodes_and_keeps_connectors_and_grid() -> None:
    result = normalize_content_cards(
        [
            {
                "title": "Migration pipeline",
                "sections": [
                    {"type": "flow", "nodes": [{"title": "Only one"}]},
                    {
                        "type": "flow",
                        "nodes": [
                            {"title": "Source ruleset", "subtitle": "Altium specifications"},
                            {"name": "Constraint IR", "subtitle": "Typed rules"},
                        ],
                        "connectors": ["translates to"],
                        "layout": "grid",
                    },
                ],
            }
        ]
    )

    card = result["response_blocks"][0]
    types = [section["type"] for section in card["sections"]]
    assert types == ["flow"]
    flow = card["sections"][0]
    assert flow["nodes"][0] == {"title": "Source ruleset", "subtitle": "Altium specifications"}
    assert flow["nodes"][1]["title"] == "Constraint IR"
    assert flow["connectors"] == ["translates to"]
    assert flow["layout"] == "grid"
    assert "Source ruleset → Constraint IR" in card["fallback_text"]
    assert "diagram" in result["covers"]


def test_flow_stack_layout_is_implicit_and_connector_labels_may_repeat() -> None:
    result = normalize_content_cards(
        [
            {
                "title": "Steps",
                "sections": [
                    {
                        "type": "flow",
                        "nodes": [{"title": "A"}, {"title": "B"}, {"title": "C"}],
                        "connectors": ["then", "then"],
                    }
                ],
            }
        ]
    )

    flow = result["response_blocks"][0]["sections"][0]
    assert "layout" not in flow
    assert flow["connectors"] == ["then", "then"]


def test_concepts_section_normalizes_icons_and_bodies() -> None:
    result = normalize_content_cards(
        [
            {
                "title": "Parallelism options",
                "sections": [
                    {
                        "type": "concepts",
                        "items": [
                            {"icon": "Network", "title": "Expert parallelism", "body": "Assign MoE experts to GPUs."},
                            {"title": "Pipeline parallelism"},
                        ],
                    }
                ],
            }
        ]
    )

    assert "breakdown" in result["covers"]
    concepts = result["response_blocks"][0]["sections"][0]
    assert concepts["items"][0] == {
        "icon": "network",
        "title": "Expert parallelism",
        "body": "Assign MoE experts to GPUs.",
    }
    assert concepts["items"][1] == {"title": "Pipeline parallelism"}
    fallback = result["response_blocks"][0]["fallback_text"]
    assert "Expert parallelism: Assign MoE experts to GPUs." in fallback


def test_metric_stats_without_values_are_dropped() -> None:
    result = normalize_content_cards(
        [
            {
                "title": "Grid",
                "sections": [
                    {
                        "type": "metrics",
                        "stats": [
                            {"label": "No value"},
                            "garbage",
                            {"value": "191 tok/s"},
                        ],
                    }
                ],
            }
        ]
    )

    metrics = result["response_blocks"][0]["sections"][0]
    assert metrics["stats"] == [{"label": "", "value": "191 tok/s"}]
