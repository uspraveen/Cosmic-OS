"""Google Docs Agent - user-owned Google Docs and Drive specialist for COSMIC."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from shared import utcnow
from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope
from shared.sqlite_client import connect_sync
from shared.usage import begin_metered_call, build_usage_event, post_usage_event, serialize_usage_metadata

from .config import AGENT_ROOT, BACKEND_ROOT, GoogleDocsAgentConfig
from .doc_structure import (
    MarkdownParser,
    build_block_map,
    document_summary,
    markdown_probe_text,
    split_markdown_native_blocks,
)
from .google_docs_client import GoogleDocsClient, document_url, is_revision_conflict
from .internal_llm import invoke_google_docs_planner_llm

logger = logging.getLogger(__name__)

_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS google_docs_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT,
    intent TEXT NOT NULL,
    account_id TEXT,
    account_email TEXT,
    document_id TEXT,
    title TEXT,
    operation TEXT,
    result_summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_google_docs_runs_session_created
ON google_docs_session_runs (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS google_docs_edits (
    edit_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    session_id TEXT,
    account_id TEXT,
    account_email TEXT,
    document_id TEXT NOT NULL,
    title TEXT,
    operation TEXT NOT NULL,
    before_revision_id TEXT,
    after_revision_id TEXT,
    request_json TEXT NOT NULL,
    verification_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_google_docs_edits_doc_created
ON google_docs_edits (document_id, created_at DESC);
"""


class GoogleDocsAgent(AgentRuntime):
    RESOLVE_RESOURCE = "docs.resolve_resource"
    CREATE = "docs.create"
    READ = "docs.read"
    EDIT = "docs.edit"
    RECALL_SESSION = "docs.recall_session"

    def __init__(
        self,
        *,
        redis_client,
        config: GoogleDocsAgentConfig | None = None,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        http_client: httpx.AsyncClient | None = None,
        agent_root: str | Path | None = None,
        store_root: str | Path | None = None,
        artifacts_root: str | Path | None = None,
    ) -> None:
        self.config = config or GoogleDocsAgentConfig.from_env()
        self.agent_root = (Path(agent_root).expanduser() if agent_root else AGENT_ROOT).resolve()
        self.store_root = (Path(store_root).expanduser() if store_root else self.agent_root / "store").resolve()
        self.artifacts_root = (
            Path(artifacts_root).expanduser()
            if artifacts_root
            else BACKEND_ROOT / "runs" / "artifacts"
        ).resolve()
        self.data_root = self.store_root / "data"
        self.session_db_path = self.data_root / "google_docs_agent.db"
        self.prompts_dir = self.agent_root / "prompts"
        self.learnings_path = self.store_root / "learnings.md"
        super().__init__(
            agent_card_path=self.agent_root / "agent_card.yaml",
            redis_client=redis_client,
            instance_id=instance_id,
            agent_secret=agent_secret,
            registry_db_path=registry_db_path,
            gateway_url=self.config.gateway_url,
            gateway_internal_token=self.config.gateway_internal_token,
            http_client=http_client,
        )

    async def on_startup(self) -> None:
        self.store_root.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        (self.agent_root / "runtime" / "cache").mkdir(parents=True, exist_ok=True)
        (self.agent_root / "runtime" / "logs").mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        if not self.learnings_path.exists():
            self.learnings_path.write_text("# Google Docs Agent - Learnings\n", encoding="utf-8")
        with connect_sync(self.session_db_path) as conn:
            conn.executescript(_SESSIONS_SQL)
            conn.commit()

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        started = time.perf_counter()
        metered = begin_metered_call(prefix="google_docs_api")
        handler = getattr(self, f"handle_{task.intent.replace('.', '_')}", None)
        if not handler:
            result = self._err("INVALID_INPUT", f"Unknown intent: {task.intent}", False, "escalate")
            await self._post_specialist_usage(task, metered, result, started)
            return result
        try:
            result = await handler(task)
            if result.status == "completed":
                self._save_session(task, result.output)
            await self._post_specialist_usage(task, metered, result, started)
            return result
        except PermissionError:
            result = await self._handle_auth_error(task)
            await self._post_specialist_usage(task, metered, result, started)
            return result
        except ValueError as exc:
            result = self._err("INVALID_INPUT", str(exc), False, "escalate")
            await self._post_specialist_usage(task, metered, result, started)
            return result
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                result = await self._handle_auth_error(task)
                await self._post_specialist_usage(task, metered, result, started)
                return result
            if is_revision_conflict(exc):
                result = self._err(
                    "REVISION_CONFLICT",
                    "The Google Doc changed during the operation. Refresh and retry.",
                    True,
                    "retry",
                )
                await self._post_specialist_usage(task, metered, result, started)
                return result
            detail = self._http_status_error_detail(exc)
            result = self._err(
                "GOOGLE_API_ERROR",
                detail,
                exc.response.status_code in {408, 409, 429, 500, 502, 503, 504},
                "retry" if exc.response.status_code in {408, 409, 429, 500, 502, 503, 504} else "escalate",
            )
            await self._post_specialist_usage(task, metered, result, started)
            return result
        except httpx.TimeoutException:
            result = self._err("TIMEOUT", "Google Docs/Drive API timed out.", True, "retry")
            await self._post_specialist_usage(task, metered, result, started)
            return result
        except Exception as exc:
            if is_revision_conflict(exc):
                result = self._err(
                    "REVISION_CONFLICT",
                    "The Google Doc changed during the operation. Refresh and retry.",
                    True,
                    "retry",
                )
                await self._post_specialist_usage(task, metered, result, started)
                return result
            logger.exception(
                "google_docs_agent.error task_id=%s intent=%s elapsed_ms=%.1f",
                task.task_id,
                task.intent,
                (time.perf_counter() - started) * 1000,
            )
            result = self._err("INTERNAL_ERROR", str(exc), False, "escalate")
            await self._post_specialist_usage(task, metered, result, started)
            return result

    async def handle_docs_resolve_resource(self, task: TaskEnvelope) -> AgentResult:
        task = await self._apply_internal_plan_if_needed(task, purpose="resolve")
        await self._maybe_create_plan(task, ["Resolve Google account", "Search Drive", "Return candidates"])
        client = self._client()
        query = str(task.input.get("query") or task.input.get("resource_hint") or "").strip()
        document_id = str(task.input.get("document_id") or task.input.get("file_id") or "").strip()
        max_results = self._bounded_int(task.input.get("max_results"), self.config.max_search_results, 1, 50)
        await self._maybe_step(1, "completed", "Google account resolved.")
        if document_id:
            file_info = await client.get_file(document_id)
            matches = [file_info]
        else:
            matches = await client.list_documents(query=query, max_results=max_results)
        await self._maybe_step(2, "completed", f"Found {len(matches)} candidate document(s).")
        output = {
            "status": "completed",
            "query": query,
            "account": self._account_info(),
            "matches": [self._attach_account(item) for item in matches],
            "count": len(matches),
        }
        await self._maybe_step(3, "completed", f"Returned {len(matches)} candidate document(s).")
        return AgentResult(status="completed", output=output, artifacts=[])

    async def handle_docs_create(self, task: TaskEnvelope) -> AgentResult:
        task = await self._apply_internal_plan_if_needed(task, purpose="create")
        await self._maybe_create_plan(task, ["Resolve Google account", "Create document", "Apply initial content", "Verify document"])
        client = self._client()
        title = str(task.input.get("title") or "Untitled document").strip() or "Untitled document"
        body_markdown = str(task.input.get("body_markdown") or task.input.get("content") or "").strip()
        await self._maybe_step(1, "completed", "Google account resolved.")
        created = await client.create_document(title=title)
        document_id = created["document_id"]
        await self._maybe_step(2, "completed", f"Created '{created['title']}'.")
        edit_result: dict[str, Any] | None = None
        if body_markdown:
            edit_result = await self._overwrite_document(
                client=client,
                task=task,
                document_id=document_id,
                full_markdown_text=body_markdown,
            )
            await self._maybe_step(3, "completed", "Applied initial document content.")
        else:
            await self._maybe_step(3, "skipped", "No initial body was provided.")
        document = await client.get_document(document_id)
        summary = document_summary(
            document,
            max_read_chars=self.config.max_read_chars,
            max_blocks=self.config.max_blocks,
        )
        artifact = self._write_json_artifact(task, "document_snapshot.json", summary)
        await self._maybe_step(4, "completed", "Verified the created document.")
        output = {
            "status": "completed",
            "account": self._account_info(),
            "document": {
                "document_id": document_id,
                "title": summary.get("title") or title,
                "url": document_url(document_id),
                "revision_id": summary.get("revision_id") or created.get("revision_id"),
            },
            "initial_edit": edit_result,
            "outline": summary.get("outline", [])[:40],
            "artifact_id": artifact.artifact_id,
        }
        return AgentResult(status="completed", output=output, artifacts=[artifact])

    async def handle_docs_read(self, task: TaskEnvelope) -> AgentResult:
        task = await self._apply_internal_plan_if_needed(task, purpose="read")
        await self._maybe_create_plan(task, ["Resolve Google account", "Fetch document", "Fetch comments", "Return structure"])
        client = self._client()
        document_id = await self._resolve_document_id_for_task(task, client)
        if not document_id:
            raise ValueError("document_id is required for docs.read.")
        include_comments = self._bool(task.input.get("include_comments"), False)
        max_chars = self._bounded_int(task.input.get("max_chars"), self.config.max_read_chars, 1000, 100000)
        await self._maybe_step(1, "completed", "Google account resolved.")
        document = await client.get_document(document_id)
        await self._maybe_step(2, "completed", "Fetched document structure.")
        summary = document_summary(
            document,
            max_read_chars=max_chars,
            max_blocks=self.config.max_blocks,
        )
        comments: list[dict[str, Any]] = []
        if include_comments:
            comments = await client.list_comments(document_id, max_results=self.config.max_comments)
            await self._maybe_step(3, "completed", f"Fetched {len(comments)} comment(s).")
        else:
            await self._maybe_step(3, "skipped", "Comments not requested.")
        summary["comments"] = comments
        artifact = self._write_json_artifact(task, "document_snapshot.json", summary)
        await self._maybe_step(4, "completed", f"Returned {summary.get('block_count', 0)} block(s).")
        output = {
            "status": "completed",
            "account": self._account_info(),
            "document": {
                "document_id": document_id,
                "title": summary.get("title"),
                "url": document_url(document_id),
                "revision_id": summary.get("revision_id"),
            },
            "outline": summary.get("outline", []),
            "full_text": summary.get("full_text", ""),
            "blocks": summary.get("blocks", []),
            "tables": summary.get("tables", []),
            "images": summary.get("images", []),
            "comments": comments,
            "artifact_id": artifact.artifact_id,
        }
        return AgentResult(status="completed", output=output, artifacts=[artifact])

    async def handle_docs_edit(self, task: TaskEnvelope) -> AgentResult:
        task = await self._apply_internal_plan_if_needed(task, purpose="edit")
        operation = str(task.input.get("operation") or "").strip().lower()
        if not operation:
            raise ValueError("operation is required for docs.edit.")
        if operation == "read":
            return await self.handle_docs_read(task.model_copy(update={"intent": self.READ}))
        client = self._client()
        if operation == "overwrite_doc":
            return await self._handle_overwrite(task, client)
        if operation == "replace_text":
            return await self._handle_replace_text(task, client)
        if operation == "update_block":
            return await self._handle_update_block(task, client)
        if operation == "insert_table":
            return await self._handle_insert_table(task, client)
        if operation == "insert_image":
            return await self._handle_insert_image(task, client)
        if operation == "list_comments":
            return await self._handle_list_comments(task, client)
        if operation == "add_comment":
            return await self._handle_add_comment(task, client)
        if operation == "reply_to_comment":
            return await self._handle_reply_to_comment(task, client)
        if operation in {"resolve_comment", "reopen_comment"}:
            return await self._handle_comment_resolution(task, client, resolved=operation == "resolve_comment")
        if operation == "share_file":
            return await self._handle_share_file(task, client)
        if operation == "list_permissions":
            return await self._handle_list_permissions(task, client)
        if operation == "get_link":
            document_id = self._document_id_from_input(task.input)
            if not document_id:
                raise ValueError("document_id is required for get_link.")
            return AgentResult(
                status="completed",
                output={
                    "status": "completed",
                    "operation": operation,
                    "account": self._account_info(),
                    "document_id": document_id,
                    "url": document_url(document_id),
                },
                artifacts=[],
            )
        raise ValueError(f"Unsupported docs.edit operation: {operation}")

    async def handle_docs_recall_session(self, task: TaskEnvelope) -> AgentResult:
        session_id = str(task.input.get("session_id") or task.session_id or "").strip()
        document_id = str(task.input.get("document_id") or "").strip()
        limit = self._bounded_int(task.input.get("limit"), 10, 1, 50)
        where = []
        params: list[Any] = []
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if document_id:
            where.append("document_id = ?")
            params.append(document_id)
        where_clause = "WHERE " + " AND ".join(where) if where else ""
        with connect_sync(self.session_db_path) as conn:
            runs = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT task_id, session_id, intent, account_id, account_email, document_id, title,
                           operation, result_summary_json, created_at
                    FROM google_docs_session_runs
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    [*params, limit],
                ).fetchall()
            ]
            edits = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT edit_id, task_id, session_id, account_id, account_email, document_id, title,
                           operation, before_revision_id, after_revision_id, verification_json, created_at
                    FROM google_docs_edits
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    [*params, limit],
                ).fetchall()
            ]
        for row in runs:
            row["result_summary"] = self._json_loads(row.pop("result_summary_json", "{}"))
        for row in edits:
            row["verification"] = self._json_loads(row.pop("verification_json", "{}"))
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "session_id": session_id,
                "document_id": document_id,
                "runs": runs,
                "edits": edits,
                "count": len(runs),
            },
            artifacts=[],
        )

    async def _handle_overwrite(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        await self._maybe_create_plan(task, ["Fetch document", "Replace content", "Verify result"])
        document_id = self._require_document_id(task)
        full_markdown_text = str(task.input.get("full_markdown_text") or task.input.get("body_markdown") or "").strip()
        if not full_markdown_text:
            raise ValueError("full_markdown_text is required for overwrite_doc.")
        await self._maybe_step(1, "completed", "Fetched current revision.")
        edit = await self._overwrite_document(
            client=client,
            task=task,
            document_id=document_id,
            full_markdown_text=full_markdown_text,
        )
        await self._maybe_step(2, "completed", "Submitted revision-guarded overwrite.")
        summary = await self._verify_document(client, document_id)
        await self._maybe_step(3, "completed", "Verified updated document.")
        return self._edit_result(task, "overwrite_doc", document_id, edit, summary)

    async def _handle_replace_text(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        await self._maybe_create_plan(task, ["Fetch document", "Replace text", "Verify result"])
        document_id = self._require_document_id(task)
        old_text = str(task.input.get("old_text") or "").strip()
        new_text = str(task.input.get("new_text") or "").strip()
        if not old_text:
            raise ValueError("old_text is required for replace_text.")
        document = await client.get_document(document_id)
        block_map = build_block_map(document)
        occurrence_count = sum(str(block.get("text") or "").count(old_text) for block in block_map.blocks)
        if occurrence_count == 0:
            raise ValueError(f"No occurrences of {old_text!r} found in document.")
        await self._maybe_step(1, "completed", f"Found {occurrence_count} occurrence(s).")
        revision_before = str(document.get("revisionId") or "")
        requests = [
            {
                "replaceAllText": {
                    "containsText": {"text": old_text, "matchCase": True},
                    "replaceText": new_text,
                }
            }
        ]
        response = await client.batch_update(document_id, requests, required_revision_id=revision_before)
        revision_after = await client.get_revision_id(document_id)
        changed = (
            ((response.get("replies") or [{}])[0].get("replaceAllText") or {}).get("occurrencesChanged")
            or occurrence_count
        )
        await self._maybe_step(2, "completed", f"Replaced {changed} occurrence(s).")
        summary = await self._verify_document(client, document_id)
        await self._maybe_step(3, "completed", "Verified updated document.")
        edit = {
            "operation": "replace_text",
            "revision_before": revision_before,
            "revision_after": revision_after,
            "requests": requests,
            "verification": {"occurrences_changed": changed},
        }
        self._record_edit(task, document_id, summary.get("title", ""), edit)
        return self._edit_result(task, "replace_text", document_id, edit, summary)

    async def _handle_update_block(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        await self._maybe_create_plan(task, ["Locate block", "Update block", "Verify result"])
        document_id = self._require_document_id(task)
        block_id = str(task.input.get("block_id") or "").strip()
        new_text = str(task.input.get("new_text") or "").strip()
        expected_snippet = str(task.input.get("expected_snippet") or "").strip()
        if not block_id and not expected_snippet:
            raise ValueError("block_id or expected_snippet is required for update_block.")
        if not new_text:
            raise ValueError("new_text is required for update_block.")
        document = await client.get_document(document_id)
        block_map = build_block_map(document)
        block = block_map.get_block(block_id) if block_id else None
        if not block and expected_snippet:
            block = block_map.get_block_by_content(expected_snippet)
        if not block:
            raise ValueError("Target block was not found. Run docs.read for current block IDs.")
        if expected_snippet:
            actual = str(block.get("text") or "").lower()
            if expected_snippet[:80].lower() not in actual:
                raise ValueError("Safety abort: expected_snippet did not match the target block.")
        await self._maybe_step(1, "completed", f"Located block {block.get('id')}.")
        start = int(block["start"])
        end = int(block["end"])
        revision_before = str(document.get("revisionId") or "")
        requests = [{"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}}]
        requests.extend(MarkdownParser.parse(new_text, start_index=start))
        await client.batch_update(document_id, requests, required_revision_id=revision_before)
        revision_after = await client.get_revision_id(document_id)
        await self._maybe_step(2, "completed", "Submitted revision-guarded block update.")
        summary = await self._verify_document(client, document_id)
        probe = markdown_probe_text(new_text) or new_text[:80]
        verified = bool(probe and probe.lower() in summary.get("full_text", "").lower())
        await self._maybe_step(3, "completed", "Verified updated document.")
        edit = {
            "operation": "update_block",
            "revision_before": revision_before,
            "revision_after": revision_after,
            "requests": requests,
            "verification": {
                "block_id": block.get("id"),
                "probe": probe,
                "verified": verified,
            },
        }
        self._record_edit(task, document_id, summary.get("title", ""), edit)
        return self._edit_result(task, "update_block", document_id, edit, summary)

    async def _handle_insert_table(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        await self._maybe_create_plan(task, ["Find insertion point", "Insert table", "Fill table cells", "Verify result"])
        document_id = self._require_document_id(task)
        data = task.input.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], list) or not data[0]:
            raise ValueError("data must be a non-empty 2D array for insert_table.")
        rows = [[str(cell) for cell in row] for row in data]
        column_count = len(rows[0])
        for idx, row in enumerate(rows):
            if len(row) != column_count:
                raise ValueError(f"Row {idx} has {len(row)} column(s); expected {column_count}.")
        document = await client.get_document(document_id)
        insertion_index = self._find_insert_index(document, str(task.input.get("after_text") or "").strip())
        revision_before = str(document.get("revisionId") or "")
        await self._maybe_step(1, "completed", f"Insertion index: {insertion_index}.")
        edit_details = await self._insert_native_table_at_index(
            client=client,
            document_id=document_id,
            rows=rows,
            insertion_index=insertion_index,
            required_revision_id=revision_before,
            has_header=self._bool(task.input.get("has_header"), True),
        )
        await self._maybe_step(2, "completed", f"Inserted {len(rows)}x{column_count} table.")
        await self._maybe_step(3, "completed", "Filled table cells.")
        summary = await self._verify_document(client, document_id)
        await self._maybe_step(4, "completed", "Verified updated document.")
        edit = {
            "operation": "insert_table",
            "revision_before": revision_before,
            "revision_after": edit_details.get("revision_after"),
            "requests": edit_details.get("requests", []),
            "verification": {"rows": len(rows), "columns": column_count, "table_count": summary.get("table_count")},
        }
        self._record_edit(task, document_id, summary.get("title", ""), edit)
        return self._edit_result(task, "insert_table", document_id, edit, summary)

    async def _insert_native_table_at_index(
        self,
        *,
        client: GoogleDocsClient,
        document_id: str,
        rows: list[list[str]],
        insertion_index: int,
        required_revision_id: str = "",
        has_header: bool = True,
    ) -> dict[str, Any]:
        if not rows or not rows[0]:
            raise ValueError("rows must be a non-empty 2D array.")
        column_count = len(rows[0])
        for idx, row in enumerate(rows):
            if len(row) != column_count:
                raise ValueError(f"Row {idx} has {len(row)} column(s); expected {column_count}.")

        insert_request = {
            "insertTable": {
                "rows": len(rows),
                "columns": column_count,
                "location": {"index": insertion_index},
            }
        }
        await client.batch_update(
            document_id,
            [insert_request],
            required_revision_id=required_revision_id,
        )

        after_table = await client.get_document(document_id)
        cell_positions = self._table_cell_positions(
            after_table,
            len(rows),
            column_count,
            min_start_index=max(1, insertion_index - 5),
        )
        fill_requests: list[dict[str, Any]] = []
        for r_idx, row in enumerate(rows):
            for c_idx, cell in enumerate(row):
                pos = cell_positions.get((r_idx, c_idx))
                if pos is None or not cell:
                    continue
                fill_requests.append({"insertText": {"location": {"index": pos}, "text": cell}})
        fill_requests.sort(
            key=lambda item: item.get("insertText", {}).get("location", {}).get("index") or 0,
            reverse=True,
        )
        if fill_requests:
            await client.batch_update(
                document_id,
                fill_requests,
                required_revision_id=str(after_table.get("revisionId") or ""),
            )

        style_requests: list[dict[str, Any]] = []
        if has_header:
            styled_doc = await client.get_document(document_id)
            table_element = self._find_table_element(
                styled_doc,
                len(rows),
                column_count,
                min_start_index=max(1, insertion_index - 5),
            )
            style_requests = self._table_header_style_requests(table_element)
            if style_requests:
                try:
                    await client.batch_update(
                        document_id,
                        style_requests,
                        required_revision_id=str(styled_doc.get("revisionId") or ""),
                    )
                except httpx.HTTPStatusError as exc:
                    logger.warning(
                        "Skipping optional Google Docs table header styling for %s: %s",
                        document_id,
                        self._http_status_error_detail(exc),
                    )
                    style_requests = []

        revision_after = await client.get_revision_id(document_id)
        return {
            "revision_after": revision_after,
            "requests": [insert_request, *fill_requests, *style_requests],
            "table": {"rows": len(rows), "columns": column_count},
        }

    async def _handle_insert_image(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        await self._maybe_create_plan(task, ["Find insertion point", "Insert image", "Verify result"])
        document_id = self._require_document_id(task)
        image_url = str(task.input.get("image_url") or "").strip()
        if not image_url.startswith(("http://", "https://")):
            raise ValueError("image_url must be a public http(s) URL.")
        if len(image_url) > 2000:
            raise ValueError("image_url is too long for Google Docs image insertion.")
        document = await client.get_document(document_id)
        insertion_index = self._find_insert_index(document, str(task.input.get("after_text") or "").strip())
        revision_before = str(document.get("revisionId") or "")
        await self._maybe_step(1, "completed", f"Insertion index: {insertion_index}.")
        insert_req: dict[str, Any] = {"insertInlineImage": {"uri": image_url, "location": {"index": insertion_index}}}
        object_size: dict[str, Any] = {}
        width_pt = task.input.get("width_pt")
        height_pt = task.input.get("height_pt")
        if width_pt is not None:
            object_size["width"] = {"magnitude": float(width_pt), "unit": "PT"}
        if height_pt is not None:
            object_size["height"] = {"magnitude": float(height_pt), "unit": "PT"}
        if object_size:
            insert_req["insertInlineImage"]["objectSize"] = object_size
        response = await client.batch_update(document_id, [insert_req], required_revision_id=revision_before)
        revision_after = await client.get_revision_id(document_id)
        object_id = (((response.get("replies") or [{}])[0].get("insertInlineImage") or {}).get("objectId") or "")
        await self._maybe_step(2, "completed", "Submitted image insertion.")
        summary = await self._verify_document(client, document_id)
        verified = not object_id or any(item.get("object_id") == object_id for item in summary.get("images", []))
        await self._maybe_step(3, "completed", "Verified updated document.")
        edit = {
            "operation": "insert_image",
            "revision_before": revision_before,
            "revision_after": revision_after,
            "requests": [insert_req],
            "verification": {"object_id": object_id, "verified": verified},
        }
        self._record_edit(task, document_id, summary.get("title", ""), edit)
        return self._edit_result(task, "insert_image", document_id, edit, summary)

    async def _handle_list_comments(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        document_id = self._require_document_id(task)
        comments = await client.list_comments(document_id, max_results=self.config.max_comments)
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "operation": "list_comments",
                "account": self._account_info(),
                "document_id": document_id,
                "comments": comments,
                "count": len(comments),
            },
            artifacts=[],
        )

    async def _handle_add_comment(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        document_id = self._require_document_id(task)
        content = str(task.input.get("content") or task.input.get("comment") or "").strip()
        quoted_text = str(task.input.get("quoted_text") or task.input.get("anchor_text") or "").strip()
        comment = await client.create_comment(document_id, content=content, quoted_text=quoted_text)
        self._record_edit(
            task,
            document_id,
            "",
            {
                "operation": "add_comment",
                "revision_before": "",
                "revision_after": "",
                "requests": [{"comment": {"content": content, "quoted_text": quoted_text}}],
                "verification": {"comment_id": comment.get("comment_id")},
            },
        )
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "operation": "add_comment",
                "account": self._account_info(),
                "document_id": document_id,
                "comment": comment,
            },
            artifacts=[],
        )

    async def _handle_reply_to_comment(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        document_id = self._require_document_id(task)
        comment_id = str(task.input.get("comment_id") or "").strip()
        content = str(task.input.get("content") or task.input.get("reply") or "").strip()
        reply = await client.reply_to_comment(document_id, comment_id, content=content)
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "operation": "reply_to_comment",
                "account": self._account_info(),
                "document_id": document_id,
                "comment_id": comment_id,
                "reply": reply,
            },
            artifacts=[],
        )

    async def _handle_comment_resolution(
        self,
        task: TaskEnvelope,
        client: GoogleDocsClient,
        *,
        resolved: bool,
    ) -> AgentResult:
        document_id = self._require_document_id(task)
        comment_id = str(task.input.get("comment_id") or "").strip()
        comment = await client.update_comment_resolved(document_id, comment_id, resolved=resolved)
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "operation": "resolve_comment" if resolved else "reopen_comment",
                "account": self._account_info(),
                "document_id": document_id,
                "comment": comment,
            },
            artifacts=[],
        )

    async def _handle_share_file(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        document_id = self._require_document_id(task)
        role = str(task.input.get("role") or "reader").strip().lower()
        permission_type = str(task.input.get("type") or task.input.get("permission_type") or "user").strip().lower()
        email_address = str(task.input.get("email_address") or "").strip()
        domain = str(task.input.get("domain") or "").strip()
        sensitive = role in {"writer", "commenter", "owner"} or permission_type in {"domain", "anyone"}
        if sensitive and not self._bool(task.input.get("approval_confirmed"), False):
            return self._err(
                "APPROVAL_REQUIRED",
                "Sharing with writer/commenter access or broad visibility requires explicit user approval.",
                False,
                "user_approval",
            )
        permission = await client.create_permission(
            document_id,
            role=role,
            permission_type=permission_type,
            email_address=email_address,
            domain=domain,
            send_notification_email=self._bool(task.input.get("send_notification_email"), True),
        )
        permissions = await client.list_permissions(document_id)
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "operation": "share_file",
                "account": self._account_info(),
                "document_id": document_id,
                "url": document_url(document_id),
                "permission": permission,
                "permissions": permissions,
            },
            artifacts=[],
        )

    async def _handle_list_permissions(self, task: TaskEnvelope, client: GoogleDocsClient) -> AgentResult:
        document_id = self._require_document_id(task)
        permissions = await client.list_permissions(document_id)
        return AgentResult(
            status="completed",
            output={
                "status": "completed",
                "operation": "list_permissions",
                "account": self._account_info(),
                "document_id": document_id,
                "permissions": permissions,
                "count": len(permissions),
            },
            artifacts=[],
        )

    async def _overwrite_document(
        self,
        *,
        client: GoogleDocsClient,
        task: TaskEnvelope,
        document_id: str,
        full_markdown_text: str,
    ) -> dict[str, Any]:
        del task
        document = await client.get_document(document_id)
        content = ((document.get("body") or {}).get("content") or [])
        revision_before = str(document.get("revisionId") or "")
        native_blocks = split_markdown_native_blocks(full_markdown_text)
        contains_native_tables = any(block.get("type") == "table" for block in native_blocks)
        requests: list[dict[str, Any]] = []
        if content:
            end_index = int(content[-1].get("endIndex") or 1) - 1
            if end_index > 1:
                requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index}}})

        if not contains_native_tables:
            requests.extend(MarkdownParser.parse(full_markdown_text, start_index=1))
            await client.batch_update(document_id, requests, required_revision_id=revision_before)
            revision_after = await client.get_revision_id(document_id)
            return {
                "operation": "overwrite_doc",
                "revision_before": revision_before,
                "revision_after": revision_after,
                "requests": requests,
                "verification": {"probe": markdown_probe_text(full_markdown_text)},
            }

        applied_requests: list[dict[str, Any]] = []
        if requests:
            await client.batch_update(document_id, requests, required_revision_id=revision_before)
            applied_requests.extend(requests)

        native_table_count = 0
        for block in native_blocks:
            block_type = str(block.get("type") or "")
            if block_type == "markdown":
                markdown_text = str(block.get("text") or "").strip("\n")
                if not markdown_text.strip():
                    continue
                current_doc = await client.get_document(document_id)
                insertion_index = self._find_insert_index(current_doc)
                markdown_requests = MarkdownParser.parse(markdown_text, start_index=insertion_index)
                if markdown_requests:
                    await client.batch_update(
                        document_id,
                        markdown_requests,
                        required_revision_id=str(current_doc.get("revisionId") or ""),
                    )
                    applied_requests.extend(markdown_requests)
                continue
            if block_type == "table":
                rows = block.get("rows")
                if not isinstance(rows, list) or not rows:
                    continue
                table_rows = [[str(cell) for cell in row] for row in rows if isinstance(row, list)]
                if not table_rows or not table_rows[0]:
                    continue
                current_doc = await client.get_document(document_id)
                insertion_index = self._find_insert_index(current_doc)
                table_edit = await self._insert_native_table_at_index(
                    client=client,
                    document_id=document_id,
                    rows=table_rows,
                    insertion_index=insertion_index,
                    required_revision_id=str(current_doc.get("revisionId") or ""),
                    has_header=self._bool(block.get("has_header"), True),
                )
                applied_requests.extend(table_edit.get("requests", []))
                native_table_count += 1

        revision_after = await client.get_revision_id(document_id)
        return {
            "operation": "overwrite_doc",
            "revision_before": revision_before,
            "revision_after": revision_after,
            "requests": applied_requests,
            "verification": {
                "probe": markdown_probe_text(full_markdown_text),
                "native_table_count": native_table_count,
            },
        }

    async def _verify_document(self, client: GoogleDocsClient, document_id: str) -> dict[str, Any]:
        document = await client.get_document(document_id)
        return document_summary(
            document,
            max_read_chars=self.config.max_read_chars,
            max_blocks=self.config.max_blocks,
        )

    def _edit_result(
        self,
        task: TaskEnvelope,
        operation: str,
        document_id: str,
        edit: dict[str, Any],
        summary: dict[str, Any],
    ) -> AgentResult:
        artifact = self._write_json_artifact(task, "document_snapshot.json", summary)
        output = {
            "status": "completed",
            "operation": operation,
            "account": self._account_info(),
            "document": {
                "document_id": document_id,
                "title": summary.get("title"),
                "url": document_url(document_id),
                "revision_id": summary.get("revision_id"),
            },
            "revision_before": edit.get("revision_before"),
            "revision_after": edit.get("revision_after"),
            "verification": edit.get("verification") or {},
            "outline": summary.get("outline", [])[:60],
            "artifact_id": artifact.artifact_id,
        }
        return AgentResult(status="completed", output=output, artifacts=[artifact])

    def _record_edit(
        self,
        task: TaskEnvelope,
        document_id: str,
        title: str,
        edit: dict[str, Any],
    ) -> None:
        account = self._account_info()
        try:
            edit_id = f"ged_{hashlib.sha256(f'{task.task_id}:{time.time_ns()}'.encode()).hexdigest()[:16]}"
            with connect_sync(self.session_db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO google_docs_edits
                    (edit_id, task_id, session_id, account_id, account_email, document_id, title,
                     operation, before_revision_id, after_revision_id, request_json, verification_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        edit_id,
                        task.task_id,
                        task.session_id,
                        account.get("account_id"),
                        account.get("account_email"),
                        document_id,
                        title,
                        edit.get("operation"),
                        edit.get("revision_before"),
                        edit.get("revision_after"),
                        json.dumps(edit.get("requests") or [], ensure_ascii=False),
                        json.dumps(edit.get("verification") or {}, ensure_ascii=False),
                        utcnow().isoformat(),
                    ],
                )
                conn.commit()
        except Exception:
            logger.warning("google_docs_agent.edit_save_failed task_id=%s", task.task_id, exc_info=True)

    def _save_session(self, task: TaskEnvelope, output: dict[str, Any]) -> None:
        document = output.get("document") if isinstance(output.get("document"), dict) else {}
        document_id = str(
            output.get("document_id")
            or document.get("document_id")
            or task.input.get("document_id")
            or task.input.get("file_id")
            or ""
        ).strip()
        title = str(output.get("title") or document.get("title") or "").strip()
        operation = str(output.get("operation") or task.input.get("operation") or "").strip()
        account = self._account_info()
        try:
            with connect_sync(self.session_db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO google_docs_session_runs
                    (task_id, session_id, intent, account_id, account_email, document_id, title,
                     operation, result_summary_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        task.task_id,
                        task.session_id,
                        task.intent,
                        account.get("account_id"),
                        account.get("account_email"),
                        document_id,
                        title,
                        operation,
                        json.dumps(self._summary_for_output(output), ensure_ascii=False),
                        utcnow().isoformat(),
                    ],
                )
                conn.commit()
        except Exception:
            logger.warning("google_docs_agent.session_save_failed task_id=%s", task.task_id, exc_info=True)

    def _summary_for_output(self, output: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": output.get("status"),
            "account": output.get("account"),
            "operation": output.get("operation"),
            "count": output.get("count"),
            "document": output.get("document"),
            "artifact_id": output.get("artifact_id"),
        }

    async def _apply_internal_plan_if_needed(self, task: TaskEnvelope, *, purpose: str) -> TaskEnvelope:
        if not self._should_use_internal_planner(task, purpose=purpose):
            return task
        try:
            document_context = await self._planner_document_context(task, purpose=purpose)
            payload = self._planner_payload(task, purpose=purpose, document_context=document_context)
            plan = await invoke_google_docs_planner_llm(
                cfg=self.config,
                http_client=self._http_client,
                user_payload=payload,
                task_context=self._task_context(task),
            )
        except Exception as exc:
            logger.warning("google_docs_agent.internal_planner_failed task_id=%s error=%s", task.task_id, exc)
            return task

        if self._bool(plan.get("needs_clarification"), False):
            question = str(plan.get("clarifying_question") or "").strip()
            if question:
                raise ValueError(f"Clarification needed: {question}")
            raise ValueError("Clarification needed before editing the Google Doc.")

        params = plan.get("params") if isinstance(plan.get("params"), dict) else {}
        merged = dict(task.input)
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, list) and not value:
                continue
            if isinstance(value, dict) and not value:
                continue
            if key not in merged or merged.get(key) in (None, "", [], {}):
                merged[key] = value

        operation = str(plan.get("operation") or "").strip()
        intent = str(plan.get("intent") or "").strip()
        if purpose == "create" and operation == "create":
            merged.setdefault("title", params.get("title") or "Untitled document")
            if params.get("body_markdown") and not merged.get("body_markdown"):
                merged["body_markdown"] = params["body_markdown"]
        elif purpose == "edit" and operation and operation not in {"create", "resolve_resource"}:
            if not str(merged.get("operation") or "").strip() or str(merged.get("operation")).strip().lower() in {"auto", "plan"}:
                merged["operation"] = operation
        elif purpose == "read" and intent == self.READ:
            merged.setdefault("include_comments", bool(params.get("include_comments", False)))
        elif purpose == "resolve" and operation == "resolve_resource":
            merged.setdefault("query", params.get("query") or params.get("resource_hint") or "")

        merged["internal_llm_plan"] = {
            "intent": intent,
            "operation": operation,
            "confidence": plan.get("confidence"),
            "needs_approval": plan.get("needs_approval"),
            "approval_reason": plan.get("approval_reason"),
            "reasoning": plan.get("reasoning"),
        }
        return task.model_copy(update={"input": merged})

    def _should_use_internal_planner(self, task: TaskEnvelope, *, purpose: str) -> bool:
        if not self.config.enable_internal_llm:
            return False
        if not self.config.internal_llm_api_key or not self.config.internal_llm_base_url:
            return False
        input_data = task.input if isinstance(task.input, dict) else {}
        natural_keys = (
            "query",
            "user_request",
            "natural_language_request",
            "instructions",
            "goal",
            "request",
        )
        has_natural_request = any(str(input_data.get(key) or "").strip() for key in natural_keys)
        if purpose == "edit":
            operation = str(input_data.get("operation") or "").strip().lower()
            return has_natural_request and (not operation or operation in {"auto", "plan"})
        if purpose == "create":
            return has_natural_request and (
                not str(input_data.get("title") or "").strip()
                or not str(input_data.get("body_markdown") or input_data.get("content") or "").strip()
            )
        if purpose == "read":
            return has_natural_request and not self._document_id_from_input(input_data)
        if purpose == "resolve":
            return has_natural_request and not str(input_data.get("query") or input_data.get("resource_hint") or "").strip()
        return False

    def _planner_payload(
        self,
        task: TaskEnvelope,
        *,
        purpose: str,
        document_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        input_data = dict(task.input or {})
        input_data.pop("auth", None)
        return {
            "purpose": purpose,
            "task": {
                "intent": task.intent,
                "input": input_data,
                "task_id": task.task_id,
                "session_id": task.session_id,
                "channel": task.channel,
                "source": task.source,
                "created_at": task.created_at.isoformat() if task.created_at else "",
            },
            "account": self._account_info(),
            "current_document_context": document_context or {},
            "recent_google_docs_work": self._recent_session_context(task, limit=5),
            "executor_capabilities": {
                "read": ["resolve_resource", "read", "list_comments", "list_permissions", "get_link"],
                "write": [
                    "create",
                    "overwrite_doc",
                    "replace_text",
                    "update_block",
                    "insert_table",
                    "insert_image",
                    "add_comment",
                    "reply_to_comment",
                    "resolve_comment",
                    "reopen_comment",
                    "share_file",
                ],
                "unsupported_from_old_agent": [
                    "delete_image",
                    "replace_image",
                    "read_tables",
                    "update_table_cell",
                    "add_table_rows",
                    "delete_table_rows",
                    "list_suggestions",
                    "accept_suggestion",
                    "reject_suggestion",
                    "rollback_last_edit",
                ],
            },
        }

    async def _planner_document_context(self, task: TaskEnvelope, *, purpose: str) -> dict[str, Any]:
        if purpose not in {"edit", "read"}:
            return {}
        document_id = self._document_id_from_input(task.input)
        if not document_id:
            return {}
        try:
            document = await self._client().get_document(document_id)
            summary = document_summary(
                document,
                max_read_chars=min(self.config.max_read_chars, 12000),
                max_blocks=min(self.config.max_blocks, 80),
            )
            return {
                "document_id": document_id,
                "title": summary.get("title"),
                "revision_id": summary.get("revision_id"),
                "outline": summary.get("outline", [])[:40],
                "blocks": summary.get("blocks", [])[:80],
                "tables": summary.get("tables", [])[:20],
                "images": summary.get("images", [])[:20],
                "full_text_preview": str(summary.get("full_text") or "")[:12000],
            }
        except Exception:
            logger.debug("google_docs_agent.planner_doc_context_failed task_id=%s", task.task_id, exc_info=True)
            return {}

    def _recent_session_context(self, task: TaskEnvelope, *, limit: int) -> list[dict[str, Any]]:
        session_id = str(task.session_id or "").strip()
        if not session_id or not self.session_db_path.exists():
            return []
        try:
            with connect_sync(self.session_db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT task_id, intent, account_email, document_id, title, operation,
                           result_summary_json, created_at
                    FROM google_docs_session_runs
                    WHERE session_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    [session_id, limit],
                ).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["result_summary"] = self._json_loads(item.pop("result_summary_json", "{}"))
                out.append(item)
            return out
        except Exception:
            logger.debug("google_docs_agent.recent_session_context_failed task_id=%s", task.task_id, exc_info=True)
            return []

    async def _resolve_document_id_for_task(self, task: TaskEnvelope, client: GoogleDocsClient) -> str:
        document_id = self._document_id_from_input(task.input)
        if document_id:
            return document_id
        query = str(
            task.input.get("query")
            or task.input.get("resource_hint")
            or task.input.get("document_hint")
            or task.input.get("title")
            or ""
        ).strip()
        if not query:
            return ""
        matches = await client.list_documents(query=query, max_results=2)
        if len(matches) == 1:
            return str(matches[0].get("document_id") or matches[0].get("file_id") or "").strip()
        raise ValueError("Multiple or no matching Google Docs were found. Run docs.resolve_resource first.")

    async def _post_specialist_usage(self, task: TaskEnvelope, metered_call, result: AgentResult, started: float) -> None:
        if not self.config.gateway_internal_token:
            return
        operation = str(task.input.get("operation") or task.intent).strip().replace("docs.", "")
        error_code = result.error.code if result.error else None
        try:
            event = build_usage_event(
                metered_call=metered_call,
                source_component="agent",
                source_id=self.agent_id,
                task_id=task.task_id,
                parent_task_id=task.parent_task_id,
                session_id=task.session_id,
                route="google_docs",
                operation=f"agent.google_docs.{operation}",
                provider="google",
                model="google-docs-api",
                usage_kind="specialist",
                raw_usage=None,
                success=result.status == "completed",
                error_code=error_code,
                latency_ms=int((time.perf_counter() - started) * 1000),
                metadata_json=serialize_usage_metadata(
                    {
                        "intent": task.intent,
                        "channel": task.channel,
                        "source": task.source,
                        "source_id": task.source_id,
                        "account": self._account_info(),
                    }
                ),
            )
            await post_usage_event(
                client=self._http_client,
                gateway_url=self.config.gateway_url,
                internal_token=self.config.gateway_internal_token,
                event=event,
            )
        except Exception:
            logger.debug("google_docs_agent.specialist_usage_post_failed task_id=%s", task.task_id, exc_info=True)

    def _task_context(self, task: TaskEnvelope) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "parent_task_id": task.parent_task_id,
            "session_id": task.session_id,
            "source": task.source,
            "source_id": task.source_id,
            "channel": task.channel,
        }

    def _client(self) -> GoogleDocsClient:
        return GoogleDocsClient(self._require_auth(), timeout_sec=self.config.request_timeout_sec)

    def _require_auth(self) -> str:
        if not self.auth or not self.auth.get("access_token"):
            raise PermissionError("No Google credentials provided for Google Docs operation.")
        return str(self.auth["access_token"])

    async def _handle_auth_error(self, task: TaskEnvelope) -> AgentResult:
        if self.auth and self.auth.get("credential_ref"):
            try:
                await self.submit_reverse_task(
                    current_task=task,
                    intent="orchestrator.refresh_credential",
                    input_payload={
                        "credential_ref": self.auth.get("credential_ref", ""),
                        "provider": "google",
                        "parent_task_id": task.task_id,
                    },
                )
            except Exception:
                logger.exception("google_docs_agent.credential_refresh_request_failed task_id=%s", task.task_id)
            return self._err("AUTH_ERROR", "Google Docs credential expired. Requested refresh.", True, "retry")
        return self._err("AUTH_ERROR", "No Google credentials available for Google Docs operation.", False, "escalate")

    def _require_document_id(self, task: TaskEnvelope) -> str:
        document_id = self._document_id_from_input(task.input)
        if not document_id:
            raise ValueError("document_id is required.")
        return document_id

    @staticmethod
    def _document_id_from_input(input_data: dict[str, Any]) -> str:
        resource = input_data.get("resource") if isinstance(input_data.get("resource"), dict) else {}
        return str(
            input_data.get("document_id")
            or input_data.get("doc_id")
            or input_data.get("file_id")
            or resource.get("document_id")
            or resource.get("file_id")
            or ""
        ).strip()

    def _find_insert_index(self, document: dict[str, Any], after_text: str = "") -> int:
        content = ((document.get("body") or {}).get("content") or [])
        block_map = build_block_map(document)
        if after_text:
            block = block_map.get_block_by_content(after_text)
            if block:
                return int(block.get("end") or 1)
        if content:
            return max(1, int(content[-1].get("endIndex") or 2) - 1)
        return 1

    def _table_cell_positions(
        self,
        document: dict[str, Any],
        rows: int,
        columns: int,
        *,
        min_start_index: int | None = None,
    ) -> dict[tuple[int, int], int]:
        table_element = self._find_table_element(
            document,
            rows,
            columns,
            min_start_index=min_start_index,
        )
        candidate = (
            (table_element.get("table") or {}).get("tableRows") or []
            if isinstance(table_element, dict)
            else None
        )
        positions: dict[tuple[int, int], int] = {}
        if candidate is None:
            return positions
        for r_idx, row in enumerate(candidate):
            for c_idx, cell in enumerate(row.get("tableCells") or []):
                cell_content = cell.get("content") or []
                if not cell_content:
                    continue
                first = cell_content[0]
                start = first.get("startIndex")
                if isinstance(start, int):
                    positions[(r_idx, c_idx)] = start
        return positions

    def _find_table_element(
        self,
        document: dict[str, Any],
        rows: int,
        columns: int,
        *,
        min_start_index: int | None = None,
    ) -> dict[str, Any] | None:
        del self
        content = ((document.get("body") or {}).get("content") or [])
        candidates: list[dict[str, Any]] = []
        for element in content:
            table = element.get("table") if isinstance(element, dict) else None
            if not isinstance(table, dict):
                continue
            table_rows = table.get("tableRows") or []
            if len(table_rows) == rows and all(len(row.get("tableCells") or []) == columns for row in table_rows):
                candidates.append(element)
        if not candidates:
            return None
        if min_start_index is None:
            return candidates[-1]
        viable = [
            element
            for element in candidates
            if int(element.get("startIndex") or 0) >= int(min_start_index)
        ]
        if viable:
            return min(viable, key=lambda item: int(item.get("startIndex") or 0))
        return min(
            candidates,
            key=lambda item: abs(int(item.get("startIndex") or 0) - int(min_start_index)),
        )

    def _table_header_style_requests(
        self,
        table_element: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        del self
        if not isinstance(table_element, dict):
            return []
        table = table_element.get("table") if isinstance(table_element.get("table"), dict) else {}
        rows = table.get("tableRows") or []
        if not rows:
            return []
        first_row = rows[0]
        cells = first_row.get("tableCells") or []
        requests: list[dict[str, Any]] = []
        table_start = table_element.get("startIndex")
        if isinstance(table_start, int) and cells:
            requests.append(
                {
                    "updateTableCellStyle": {
                        "tableRange": {
                            "tableCellLocation": {
                                "tableStartLocation": {"index": table_start},
                                "rowIndex": 0,
                                "columnIndex": 0,
                            },
                            "rowSpan": 1,
                            "columnSpan": len(cells),
                        },
                        "tableCellStyle": {
                            "backgroundColor": {
                                "color": {
                                    "rgbColor": {
                                        "red": 0.93,
                                        "green": 0.95,
                                        "blue": 0.98,
                                    }
                                }
                            }
                        },
                        "fields": "backgroundColor",
                    }
                }
            )

        for cell in cells:
            ranges = []
            for cell_element in cell.get("content") or []:
                paragraph = cell_element.get("paragraph") if isinstance(cell_element, dict) else None
                if not isinstance(paragraph, dict):
                    continue
                for item in paragraph.get("elements") or []:
                    text_run = item.get("textRun") if isinstance(item, dict) else None
                    content = str((text_run or {}).get("content") or "")
                    if not content.strip():
                        continue
                    start = item.get("startIndex")
                    end = item.get("endIndex")
                    if isinstance(start, int) and isinstance(end, int) and end > start:
                        ranges.append((start, end - 1))
            if ranges:
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": min(start for start, _ in ranges),
                                "endIndex": max(end for _, end in ranges),
                            },
                            "textStyle": {"bold": True},
                            "fields": "bold",
                        }
                    }
                )
        return requests

    @staticmethod
    def _http_status_error_detail(exc: httpx.HTTPStatusError) -> str:
        status_code = exc.response.status_code
        detail = ""
        try:
            payload = exc.response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                status = str(error.get("status") or "").strip()
                reason = ""
                details = error.get("details")
                if isinstance(details, list) and details:
                    first = details[0]
                    if isinstance(first, dict):
                        reason = str(first.get("reason") or first.get("message") or "").strip()
                detail_parts = [part for part in (status, message, reason) if part]
                detail = ": ".join(detail_parts)
            else:
                detail = str(payload.get("message") or payload.get("error_description") or "").strip()
        if not detail:
            detail = (exc.response.text or "").strip()
        if detail:
            detail = " ".join(detail.split())
            if len(detail) > 700:
                detail = detail[:697].rstrip() + "..."
            return f"Google Docs/Drive API error: {status_code}: {detail}"
        return f"Google Docs/Drive API error: {status_code}"

    def _write_json_artifact(self, task: TaskEnvelope, filename: str, payload: dict[str, Any]) -> ArtifactManifest:
        safe_filename = filename.replace("/", "_").replace("\\", "_")
        output_dir = self.artifacts_root / task.task_id / "google_docs_agent"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / safe_filename
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        path.write_bytes(raw)
        return ArtifactManifest(
            artifact_id=f"art_{task.task_id}_google_docs_{hashlib.sha256(raw).hexdigest()[:12]}",
            task_id=task.task_id,
            mime="application/json",
            sha256=hashlib.sha256(raw).hexdigest(),
            path=str(path.relative_to(BACKEND_ROOT)),
            created_by_agent=self.agent_id,
            kind="output",
            audience="supporting",
        )

    async def _maybe_create_plan(self, task: TaskEnvelope, steps: list[str]) -> None:
        if self.step_plan is None or len(steps) < 3:
            return
        try:
            await self.step_plan.create(steps)
        except Exception:
            logger.debug("google_docs_agent.step_plan_create_failed task_id=%s", task.task_id, exc_info=True)

    async def _maybe_step(self, step: int, status: str, message: str) -> None:
        if self.step_plan is None:
            return
        try:
            await self.step_plan.update(step, status, message)
        except Exception:
            logger.debug("google_docs_agent.step_plan_update_failed", exc_info=True)

    def _account_info(self) -> dict[str, Any]:
        auth = self.auth if isinstance(self.auth, dict) else {}
        return {
            "account_id": auth.get("account_id"),
            "account_email": auth.get("account_email"),
            "account_label": auth.get("account_label") or auth.get("account_display_name") or auth.get("account_email"),
            "account_display_name": auth.get("account_display_name"),
            "account_is_primary": bool(auth.get("account_is_primary")),
        }

    def _attach_account(self, item: dict[str, Any]) -> dict[str, Any]:
        return {**item, **self._account_info()}

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(maximum, parsed))

    @staticmethod
    def _bool(value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _json_loads(value: str) -> dict[str, Any]:
        try:
            loaded = json.loads(value or "{}")
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _err(code: str, message: str, retryable: bool, next_action: str) -> AgentResult:
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(
                code=code,
                retryable=retryable,
                message=message,
                next_action=next_action,
            ),
        )
