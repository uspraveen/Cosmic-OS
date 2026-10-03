"""Notion tool plumbing: the approval store, the markdown-to-blocks converter,
and the id/title extraction the routes lean on.

The gateway runtime itself is too heavy to instantiate in unit tests, so the
orchestrator-facing flow is pinned here at its pure seams: whatever the card
shows must be what Approve writes, ids must survive URL round trips, and no
line of a drafted page may be silently dropped by the converter.

All tests are sync (asyncio-free) so deploy verification can run them on the
VM, whose venv has no pytest-asyncio.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from gateway.notion_approval_store import NotionApprovalStore
from gateway.notion_client import (
    block_to_text,
    extract_notion_id,
    markdown_to_notion_blocks,
    page_title,
    result_summary,
)


def _store(tmp: Path) -> NotionApprovalStore:
    store = NotionApprovalStore(tmp / "notion_approvals.db")
    store.initialize()
    return store


_PROPOSAL = {
    "operation": "create_page",
    "account_id": "acc_notion_1",
    "workspace": "Praveen's Notion",
    "parent_page_id": "8ba5a435b7c2460fa8e0f2e2a0b6c9d1",
    "title": "Weekly notes",
    "content_text": "Plan\n\n- ship the thing",
    "session_id": "sess_1",
}


class TestNotionApprovalStore:
    def test_a_proposal_parks_pending_and_keeps_its_payload(self, tmp_path) -> None:
        store = _store(tmp_path)
        row, created = store.upsert_pending(_PROPOSAL)
        assert created is True
        assert row["approval_id"].startswith("nta_")
        assert row["status"] == "pending"
        assert row["workspace"] == "Praveen's Notion"
        assert row["content_text"] == "Plan\n\n- ship the thing"
        assert row["session_id"] == "sess_1"

    def test_reproposing_the_same_write_reopens_one_card(self, tmp_path) -> None:
        """Same account + operation + target means the same card: a model that
        re-proposes after editing its draft re-opens the existing approval
        instead of stacking a second card for the same destination."""
        store = _store(tmp_path)
        first, created = store.upsert_pending(_PROPOSAL)
        second, created_again = store.upsert_pending({**_PROPOSAL, "content_text": "Revised plan"})
        assert created is True and created_again is False
        assert second["approval_id"] == first["approval_id"]
        assert second["content_text"] == "Revised plan"
        assert second["status"] == "pending"

    def test_a_different_target_gets_its_own_card(self, tmp_path) -> None:
        store = _store(tmp_path)
        first, _ = store.upsert_pending(_PROPOSAL)
        second, created = store.upsert_pending(
            {**_PROPOSAL, "parent_page_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
        )
        assert created is True
        assert second["approval_id"] != first["approval_id"]

    def test_a_completed_approval_never_flips_back_to_pending(self, tmp_path) -> None:
        """A late re-propose must not resurrect a write the user already
        approved — that would double-post the page."""
        store = _store(tmp_path)
        row, _ = store.upsert_pending(_PROPOSAL)
        store.mark_completed(row["approval_id"], {"url": "https://notion.so/x"})
        again, _ = store.upsert_pending({**_PROPOSAL, "content_text": "Changed after the fact"})
        assert again["status"] == "completed"

    def test_failed_returns_to_pending_so_approve_is_a_retry(self, tmp_path) -> None:
        store = _store(tmp_path)
        row, _ = store.upsert_pending(_PROPOSAL)
        store.mark_executing(row["approval_id"])
        failed = store.mark_failed(row["approval_id"], "Notion timed out")
        assert failed["status"] == "pending"
        assert failed["notes"] == "Notion timed out"

    def test_reject_is_terminal(self, tmp_path) -> None:
        store = _store(tmp_path)
        row, _ = store.upsert_pending(_PROPOSAL)
        rejected = store.mark_rejected(row["approval_id"], "not what I wanted")
        assert rejected["status"] == "rejected"
        assert rejected["notes"] == "not what I wanted"

    def test_list_can_exclude_terminal_rows(self, tmp_path) -> None:
        store = _store(tmp_path)
        pending, _ = store.upsert_pending(_PROPOSAL)
        done, _ = store.upsert_pending({**_PROPOSAL, "page_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "operation": "update_page"})
        store.mark_completed(done["approval_id"], {})
        live = store.list(include_terminal=False)
        assert [row["approval_id"] for row in live] == [pending["approval_id"]]

    def test_validation_rejects_unusable_proposals(self, tmp_path) -> None:
        store = _store(tmp_path)
        import pytest

        with pytest.raises(ValueError):
            store.upsert_pending({**_PROPOSAL, "operation": "delete_space"})
        with pytest.raises(ValueError):
            store.upsert_pending({**_PROPOSAL, "account_id": ""})
        with pytest.raises(ValueError):
            store.upsert_pending({**_PROPOSAL, "parent_page_id": "", "parent_database_id": ""})


class TestMarkdownToNotionBlocks:
    def test_headings_bullets_and_paragraphs_map(self) -> None:
        blocks = markdown_to_notion_blocks(
            "# Title\n\nIntro paragraph.\n\n## Section\n- first\n- second"
        )
        kinds = [block["type"] for block in blocks]
        assert kinds == ["heading_1", "paragraph", "heading_2", "bulleted_list_item", "bulleted_list_item"]
        assert blocks[0]["heading_1"]["rich_text"][0]["text"]["content"] == "Title"
        assert blocks[3]["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "first"

    def test_todos_quotes_numbered_and_code_map(self) -> None:
        blocks = markdown_to_notion_blocks(
            "- [ ] open item\n- [x] done item\n> quoted\n1. step\n```\ncode line\n```"
        )
        assert [block["type"] for block in blocks] == [
            "to_do", "to_do", "quote", "numbered_list_item", "code",
        ]
        assert blocks[0]["to_do"]["checked"] is False
        assert blocks[1]["to_do"]["checked"] is True
        assert blocks[4]["code"]["rich_text"][0]["text"]["content"] == "code line"

    def test_no_line_is_silently_dropped(self) -> None:
        """Anything unrecognized stays a paragraph — a drafted line that
        vanished between preview and page would be a silent content loss."""
        content = "plain line\n * asterisk bullet\n42. numbered\n#### deep heading"
        blocks = markdown_to_notion_blocks(content)
        texts = [block_to_text(block) for block in blocks]
        assert "plain line" in texts[0]
        assert texts[1] == "- asterisk bullet"
        assert texts[2] == "1. numbered"
        # Notion has three heading levels; #### correctly normalizes to ###.
        assert texts[3] == "### deep heading"

    def test_blank_lines_produce_no_blocks(self) -> None:
        assert markdown_to_notion_blocks("\n\n\n") == []


class TestReferenceParsing:
    def test_bare_ids_with_and_without_dashes(self) -> None:
        dashed = "8ba5a435-b7c2-460f-a8e0-f2e2a0b6c9d1"
        assert extract_notion_id(dashed) == "8ba5a435b7c2460fa8e0f2e2a0b6c9d1"
        assert extract_notion_id("8ba5a435b7c2460fa8e0f2e2a0b6c9d1") == "8ba5a435b7c2460fa8e0f2e2a0b6c9d1"

    def test_notion_urls_yield_the_page_id(self) -> None:
        url = "https://www.notion.so/My-page-8ba5a435b7c2460fa8e0f2e2a0b6c9d1?pvs=4"
        assert extract_notion_id(url) == "8ba5a435b7c2460fa8e0f2e2a0b6c9d1"
        vanity = "https://praveen.notion.site/8ba5a435b7c2460fa8e0f2e2a0b6c9d1"
        assert extract_notion_id(vanity) == "8ba5a435b7c2460fa8e0f2e2a0b6c9d1"

    def test_garbage_yields_empty_so_the_route_can_400(self) -> None:
        assert extract_notion_id("") == ""
        assert extract_notion_id("my meeting notes") == ""
        assert extract_notion_id("https://example.com/notion") == ""


class TestNotionPayloadHelpers:
    def test_page_title_reads_the_title_property(self) -> None:
        page = {
            "object": "page",
            "properties": {
                "Name": {
                    "type": "title",
                    "title": [{"plain_text": "Weekly notes"}],
                },
                "Status": {"type": "select", "select": {"name": "Active"}},
            },
        }
        assert page_title(page) == "Weekly notes"
        assert page_title({"object": "page", "properties": {}}) == ""

    def test_result_summary_normalizes_for_the_model(self) -> None:
        page = {
            "object": "page",
            "id": "8ba5a435b7c2460fa8e0f2e2a0b6c9d1",
            "url": "https://www.notion.so/x",
            "last_edited_time": "2026-10-03T00:00:00.000Z",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Notes"}]}},
            "parent": {"type": "page_id", "page_id": "p1"},
        }
        summary = result_summary(page)
        assert summary["title"] == "Notes"
        assert summary["object"] == "page"
        assert summary["last_edited_at"] == "2026-10-03T00:00:00.000Z"
        assert summary["parent"] == {"type": "page_id", "page_id": "p1"}

    def test_block_to_text_round_trips_the_converter(self) -> None:
        blocks = markdown_to_notion_blocks("## Heading\n- bullet\n> quote")
        texts = [block_to_text(block) for block in blocks]
        assert texts == ["## Heading", "- bullet", "> quote"]
