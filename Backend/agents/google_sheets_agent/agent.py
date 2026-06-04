"""Google Sheets Agent - user-owned Google Sheets and Drive specialist."""

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
from shared.contracts import AgentError, AgentResult, TaskEnvelope
from shared.sqlite_client import connect_sync
from shared.usage import begin_metered_call, build_usage_event, post_usage_event, serialize_usage_metadata

from .config import AGENT_ROOT, BACKEND_ROOT, GoogleSheetsAgentConfig
from .google_sheets_client import GoogleSheetsClient, spreadsheet_url
from .internal_llm import invoke_google_sheets_planner_llm
from .sheet_structure import SheetNavigator, count_cells, normalize_rows, rows_from_input, values_preview

logger = logging.getLogger(__name__)

_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS google_sheets_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT,
    intent TEXT NOT NULL,
    account_id TEXT,
    account_email TEXT,
    spreadsheet_id TEXT,
    title TEXT,
    operation TEXT,
    result_summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_google_sheets_runs_session_created
ON google_sheets_session_runs (session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS google_sheets_edits (
    edit_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    session_id TEXT,
    account_id TEXT,
    account_email TEXT,
    spreadsheet_id TEXT NOT NULL,
    title TEXT,
    operation TEXT NOT NULL,
    range_name TEXT,
    request_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_google_sheets_edits_sheet_created
ON google_sheets_edits (spreadsheet_id, created_at DESC);
"""


class ApprovalRequiredError(RuntimeError):
    pass


class GoogleSheetsAgent(AgentRuntime):
    RESOLVE_RESOURCE = "sheets.resolve_resource"
    CREATE = "sheets.create"
    READ = "sheets.read"
    EDIT = "sheets.edit"
    RECALL_SESSION = "sheets.recall_session"

    def __init__(
        self,
        *,
        redis_client,
        config: GoogleSheetsAgentConfig | None = None,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        http_client: httpx.AsyncClient | None = None,
        agent_root: str | Path | None = None,
        store_root: str | Path | None = None,
        artifacts_root: str | Path | None = None,
    ) -> None:
        self.config = config or GoogleSheetsAgentConfig.from_env()
        self.agent_root = (Path(agent_root).expanduser() if agent_root else AGENT_ROOT).resolve()
        self.store_root = (Path(store_root).expanduser() if store_root else self.agent_root / "store").resolve()
        self.artifacts_root = (
            Path(artifacts_root).expanduser()
            if artifacts_root
            else BACKEND_ROOT / "runs" / "artifacts"
        ).resolve()
        self.data_root = self.store_root / "data"
        self.session_db_path = self.data_root / "google_sheets_agent.db"
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
            self.learnings_path.write_text("# Google Sheets Agent - Learnings\n", encoding="utf-8")
        with connect_sync(self.session_db_path) as conn:
            conn.executescript(_SESSIONS_SQL)
            conn.commit()

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        started = time.perf_counter()
        metered = begin_metered_call(prefix="google_sheets_api")
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
        except ApprovalRequiredError as exc:
            result = self._err("APPROVAL_REQUIRED", str(exc), False, "ask_user")
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
            detail = self._http_status_error_detail(exc)
            retryable = exc.response.status_code in {408, 409, 429, 500, 502, 503, 504}
            result = self._err(
                "GOOGLE_API_ERROR",
                detail,
                retryable,
                "retry" if retryable else "escalate",
            )
            await self._post_specialist_usage(task, metered, result, started)
            return result
        except httpx.TimeoutException:
            result = self._err("TIMEOUT", "Google Sheets/Drive API timed out.", True, "retry")
            await self._post_specialist_usage(task, metered, result, started)
            return result
        except Exception as exc:
            logger.exception(
                "google_sheets_agent.error task_id=%s intent=%s elapsed_ms=%.1f",
                task.task_id,
                task.intent,
                (time.perf_counter() - started) * 1000,
            )
            result = self._err("INTERNAL_ERROR", str(exc), False, "escalate")
            await self._post_specialist_usage(task, metered, result, started)
            return result

    async def handle_sheets_resolve_resource(self, task: TaskEnvelope) -> AgentResult:
        task = await self._apply_internal_plan_if_needed(task, purpose="resolve")
        await self._create_plan(["Resolve Google account", "Search Drive", "Return spreadsheet candidates"])
        client = self._client()
        query = str(task.input.get("query") or task.input.get("resource_hint") or "").strip()
        spreadsheet_id = self._spreadsheet_id_from_input(task.input)
        max_results = self._bounded_int(task.input.get("max_results"), self.config.max_search_results, 1, 50)
        await self._step(1, "completed", "Google account resolved.")
        if spreadsheet_id:
            matches = [await client.get_file(spreadsheet_id)]
        else:
            matches = await client.list_spreadsheets(query=query, max_results=max_results)
        await self._step(2, "completed", f"Found {len(matches)} candidate spreadsheet(s).")
        output = {
            "status": "completed",
            "query": query,
            "account": self._account_info(),
            "matches": [self._attach_account(item) for item in matches],
            "count": len(matches),
        }
        await self._step(3, "completed", f"Returned {len(matches)} candidate spreadsheet(s).")
        return AgentResult(status="completed", output=output, artifacts=[])

    async def handle_sheets_create(self, task: TaskEnvelope) -> AgentResult:
        task = await self._apply_internal_plan_if_needed(task, purpose="create")
        await self._create_plan(["Resolve Google account", "Create spreadsheet", "Write initial cells", "Verify workbook"])
        client = self._client()
        title = str(task.input.get("title") or "Untitled spreadsheet").strip() or "Untitled spreadsheet"
        sheet_specs = self._sheet_specs_from_input(task.input)
        sheet_titles = [spec["title"] for spec in sheet_specs] or [str(task.input.get("sheet_name") or "Sheet1").strip() or "Sheet1"]
        await self._step(1, "completed", "Google account resolved.")
        spreadsheet = await client.create_spreadsheet(title=title, sheet_titles=sheet_titles)
        spreadsheet_id = spreadsheet["spreadsheet_id"]
        await self._step(2, "completed", f"Created '{spreadsheet['title']}'.")

        verified_ranges: list[dict[str, Any]] = []
        latest_structure = await client.get_spreadsheet(spreadsheet_id)
        navigator = SheetNavigator(latest_structure)
        for spec in sheet_specs:
            rows = normalize_rows(spec.get("values"))
            if not rows:
                continue
            self._assert_write_budget(rows)
            range_name = navigator.ensure_range(str(spec.get("range") or "A1"), default_sheet=spec["title"])
            await client.update_values(spreadsheet_id, range_name, rows)
            if self._bool(spec.get("has_header"), True):
                await self._format_header_row(client, spreadsheet_id, navigator, sheet_name=spec["title"])
            after = await client.get_values(spreadsheet_id, range_name)
            verified_ranges.append({"range": range_name, "after": after})
        await self._step(3, "completed", f"Wrote {len(verified_ranges)} populated range(s).")

        final_structure = await client.get_spreadsheet(spreadsheet_id)
        await self._step(4, "completed", "Verified workbook structure.")
        output = {
            "status": "completed",
            "operation": "create",
            "spreadsheet": self._attach_account(final_structure),
            "spreadsheet_id": spreadsheet_id,
            "title": final_structure.get("title") or spreadsheet["title"],
            "url": spreadsheet_url(spreadsheet_id),
            "account": self._account_info(),
            "verified_ranges": verified_ranges,
        }
        self._record_edit(
            task,
            spreadsheet_id,
            str(output["title"]),
            {
                "operation": "create",
                "range": "",
                "requests": {"sheet_titles": sheet_titles, "populated_ranges": [item["range"] for item in verified_ranges]},
                "before": {},
                "after": {"structure": final_structure, "ranges": verified_ranges},
            },
        )
        return AgentResult(status="completed", output=output, artifacts=[])

    async def handle_sheets_read(self, task: TaskEnvelope) -> AgentResult:
        task = await self._apply_internal_plan_if_needed(task, purpose="read")
        await self._create_plan(["Resolve Google account", "Read spreadsheet", "Return structured result"])
        client = self._client()
        spreadsheet_id = self._require_spreadsheet_id(task)
        operation = str(task.input.get("operation") or "read_structure").strip()
        await self._step(1, "completed", "Google account resolved.")
        structure = await client.get_spreadsheet(spreadsheet_id)
        navigator = SheetNavigator(structure)
        if operation in {"read_range", "range"} or task.input.get("range"):
            range_name = navigator.ensure_range(str(task.input.get("range") or "A1"), default_sheet=task.input.get("sheet_name"))
            values = await client.get_values(spreadsheet_id, range_name)
            self._assert_read_budget(values.get("values") or [])
            output = {
                "status": "completed",
                "operation": "read_range",
                "spreadsheet_id": spreadsheet_id,
                "title": structure.get("title"),
                "url": spreadsheet_url(spreadsheet_id),
                "account": self._account_info(),
                "structure": navigator.summary(),
                "range": values,
            }
            await self._step(2, "completed", f"Read {values.get('row_count', 0)} row(s).")
        else:
            output = {
                "status": "completed",
                "operation": "read_structure",
                "spreadsheet_id": spreadsheet_id,
                "title": structure.get("title"),
                "url": spreadsheet_url(spreadsheet_id),
                "account": self._account_info(),
                "structure": navigator.summary(),
            }
            await self._step(2, "completed", f"Read {len(navigator.sheets)} sheet tab(s).")
        await self._step(3, "completed", "Returned structured spreadsheet result.")
        return AgentResult(status="completed", output=output, artifacts=[])

    async def handle_sheets_edit(self, task: TaskEnvelope) -> AgentResult:
        task = await self._apply_internal_plan_if_needed(task, purpose="edit")
        operation = str(task.input.get("operation") or "auto").strip()
        if operation == "auto":
            raise ValueError("Could not infer a safe Sheets edit operation.")

        if operation in {"share_file", "list_permissions", "get_link"}:
            return await self._handle_drive_operation(task, operation)

        await self._create_plan(["Resolve Google account", "Read workbook state", "Apply operation", "Verify result"])
        client = self._client()
        spreadsheet_id = self._require_spreadsheet_id(task)
        await self._step(1, "completed", "Google account resolved.")
        structure = await client.get_spreadsheet(spreadsheet_id)
        navigator = SheetNavigator(structure)
        await self._step(2, "completed", f"Read {len(navigator.sheets)} sheet tab(s).")

        if operation == "update_cells":
            range_name = navigator.ensure_range(str(task.input.get("range") or "A1"), default_sheet=task.input.get("sheet_name"))
            rows = normalize_rows(task.input.get("values")) or rows_from_input(task.input)
            self._assert_write_budget(rows)
            before = await client.get_values(spreadsheet_id, range_name)
            response = await client.update_values(spreadsheet_id, range_name, rows)
            after = await client.get_values(spreadsheet_id, range_name)
            result_payload = {"range": range_name, "response": response, "before": before, "after": after}
        elif operation == "append_rows":
            range_name = navigator.ensure_range(str(task.input.get("range") or "A1"), default_sheet=task.input.get("sheet_name"))
            rows = normalize_rows(task.input.get("values")) or rows_from_input(task.input)
            self._assert_write_budget(rows)
            before = await client.get_values(spreadsheet_id, range_name)
            response = await client.append_values(spreadsheet_id, range_name, rows)
            after = await client.get_values(spreadsheet_id, range_name)
            result_payload = {"range": range_name, "response": response, "before": before, "after": after}
        elif operation == "clear_range":
            range_name = navigator.ensure_range(str(task.input.get("range") or ""), default_sheet=task.input.get("sheet_name"))
            before = await client.get_values(spreadsheet_id, range_name)
            response = await client.clear_values(spreadsheet_id, range_name)
            after = await client.get_values(spreadsheet_id, range_name)
            result_payload = {"range": range_name, "response": response, "before": before, "after": after}
        elif operation == "add_sheet":
            title = str(task.input.get("title") or task.input.get("sheet_name") or "").strip()
            if not title:
                raise ValueError("title or sheet_name is required for add_sheet.")
            requests = [
                {
                    "addSheet": {
                        "properties": {
                            "title": title,
                            "gridProperties": {
                                "rowCount": self._bounded_int(task.input.get("row_count"), 1000, 1, 200000),
                                "columnCount": self._bounded_int(task.input.get("column_count"), 26, 1, 18278),
                            },
                        }
                    }
                }
            ]
            response = await client.batch_update(spreadsheet_id, requests)
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": "", "response": response, "before": structure, "after": after_structure}
        elif operation == "format_header_row":
            sheet_name = str(task.input.get("sheet_name") or navigator.active_sheet).strip()
            response = await self._format_header_row(
                client,
                spreadsheet_id,
                navigator,
                sheet_name=sheet_name,
                background_color=str(task.input.get("background_color") or "#E8F0FE"),
            )
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": f"{sheet_name}!1:1", "response": response, "before": structure, "after": after_structure}
        elif operation == "format_range":
            range_name = navigator.ensure_range(str(task.input.get("range") or "").strip())
            response = await self._format_range(
                client,
                spreadsheet_id,
                navigator,
                range_name=range_name,
                input_data=task.input,
            )
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": range_name, "response": response, "before": structure, "after": after_structure}
        elif operation == "clear_formatting":
            range_name = navigator.ensure_range(str(task.input.get("range") or "").strip())
            response = await self._clear_formatting(client, spreadsheet_id, navigator, range_name=range_name)
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": range_name, "response": response, "before": structure, "after": after_structure}
        elif operation == "set_borders":
            range_name = navigator.ensure_range(str(task.input.get("range") or "").strip())
            response = await self._set_borders(
                client,
                spreadsheet_id,
                navigator,
                range_name=range_name,
                input_data=task.input,
            )
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": range_name, "response": response, "before": structure, "after": after_structure}
        elif operation in {"auto_resize", "auto_resize_columns", "auto_resize_rows"}:
            range_name = navigator.ensure_range(str(task.input.get("range") or "").strip())
            dimension = self._dimension_from_input(task.input, default="ROWS" if operation == "auto_resize_rows" else "COLUMNS")
            response = await self._auto_resize_dimension(
                client,
                spreadsheet_id,
                navigator,
                range_name=range_name,
                dimension=dimension,
            )
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": range_name, "response": response, "before": structure, "after": after_structure}
        elif operation in {"resize_dimension", "resize_columns", "resize_rows"}:
            range_name = navigator.ensure_range(str(task.input.get("range") or "").strip())
            dimension = self._dimension_from_input(task.input, default="ROWS" if operation == "resize_rows" else "COLUMNS")
            response = await self._resize_dimension(
                client,
                spreadsheet_id,
                navigator,
                range_name=range_name,
                dimension=dimension,
                pixel_size=self._bounded_int(task.input.get("pixel_size") or task.input.get("width") or task.input.get("height"), 120, 20, 1000),
            )
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": range_name, "response": response, "before": structure, "after": after_structure}
        elif operation == "freeze_panes":
            sheet_name = str(task.input.get("sheet_name") or navigator.active_sheet).strip()
            response = await self._freeze_panes(
                client,
                spreadsheet_id,
                navigator,
                sheet_name=sheet_name,
                frozen_row_count=self._bounded_int(task.input.get("frozen_row_count") or task.input.get("rows"), 1, 0, 1000),
                frozen_column_count=self._bounded_int(task.input.get("frozen_column_count") or task.input.get("columns"), 0, 0, 1000),
            )
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": sheet_name, "response": response, "before": structure, "after": after_structure}
        elif operation in {"merge_cells", "unmerge_cells"}:
            range_name = navigator.ensure_range(str(task.input.get("range") or "").strip())
            response = await self._merge_cells(
                client,
                spreadsheet_id,
                navigator,
                range_name=range_name,
                unmerge=operation == "unmerge_cells",
                merge_type=str(task.input.get("merge_type") or "MERGE_ALL").strip(),
            )
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": range_name, "response": response, "before": structure, "after": after_structure}
        elif operation == "add_banding":
            range_name = navigator.ensure_range(str(task.input.get("range") or "").strip())
            response = await self._add_banding(
                client,
                spreadsheet_id,
                navigator,
                range_name=range_name,
                input_data=task.input,
            )
            after_structure = await client.get_spreadsheet(spreadsheet_id)
            result_payload = {"range": range_name, "response": response, "before": structure, "after": after_structure}
        else:
            raise ValueError(f"Unsupported Sheets edit operation: {operation}")

        await self._step(3, "completed", f"Applied {operation}.")
        await self._step(4, "completed", "Verified resulting workbook state.")
        title = str((result_payload.get("after") or {}).get("title") or structure.get("title") or "").strip()
        self._record_edit(
            task,
            spreadsheet_id,
            title,
            {
                "operation": operation,
                "range": result_payload.get("range") or "",
                "requests": task.input,
                "before": result_payload.get("before") or {},
                "after": result_payload.get("after") or {},
            },
        )
        output = {
            "status": "completed",
            "operation": operation,
            "spreadsheet_id": spreadsheet_id,
            "title": title,
            "url": spreadsheet_url(spreadsheet_id),
            "account": self._account_info(),
            "result": self._compact_result(result_payload),
        }
        return AgentResult(status="completed", output=output, artifacts=[])

    async def handle_sheets_recall_session(self, task: TaskEnvelope) -> AgentResult:
        await self._create_plan(["Search Sheets ledger", "Return prior operations"])
        query = str(task.input.get("query") or "").strip().lower()
        session_id = str(task.input.get("session_id") or task.session_id or "").strip()
        limit = self._bounded_int(task.input.get("limit"), 10, 1, 50)
        rows: list[dict[str, Any]] = []
        with connect_sync(self.session_db_path) as conn:
            sql = (
                "SELECT task_id, session_id, intent, account_email, spreadsheet_id, title, operation, "
                "result_summary_json, created_at FROM google_sheets_session_runs "
            )
            clauses: list[str] = []
            args: list[Any] = []
            if session_id:
                clauses.append("session_id = ?")
                args.append(session_id)
            if query:
                clauses.append("(LOWER(title) LIKE ? OR LOWER(operation) LIKE ? OR LOWER(result_summary_json) LIKE ?)")
                like = f"%{query}%"
                args.extend([like, like, like])
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT ?"
            args.append(limit)
            for row in conn.execute(sql, args):
                rows.append(
                    {
                        "task_id": row[0],
                        "session_id": row[1],
                        "intent": row[2],
                        "account_email": row[3],
                        "spreadsheet_id": row[4],
                        "title": row[5],
                        "operation": row[6],
                        "summary": self._json_loads(row[7]),
                        "created_at": row[8],
                    }
                )
        await self._step(1, "completed", f"Found {len(rows)} prior Sheets operation(s).")
        await self._step(2, "completed", "Returned Sheets recall result.")
        return AgentResult(status="completed", output={"status": "completed", "matches": rows, "count": len(rows)}, artifacts=[])

    async def _handle_drive_operation(self, task: TaskEnvelope, operation: str) -> AgentResult:
        await self._create_plan(["Resolve Google account", "Apply Drive permission operation", "Return result"])
        client = self._client()
        spreadsheet_id = self._require_spreadsheet_id(task)
        await self._step(1, "completed", "Google account resolved.")
        if operation == "get_link":
            file_info = await client.get_file(spreadsheet_id)
            output = {
                "status": "completed",
                "operation": operation,
                "spreadsheet_id": spreadsheet_id,
                "url": file_info.get("url") or spreadsheet_url(spreadsheet_id),
                "file": self._attach_account(file_info),
            }
        elif operation == "list_permissions":
            permissions = await client.list_permissions(spreadsheet_id)
            output = {
                "status": "completed",
                "operation": operation,
                "spreadsheet_id": spreadsheet_id,
                "url": spreadsheet_url(spreadsheet_id),
                "account": self._account_info(),
                "permissions": permissions,
            }
        elif operation == "share_file":
            self._require_share_approval(task.input)
            permission = await client.create_permission(
                spreadsheet_id,
                role=str(task.input.get("role") or "reader").strip(),
                permission_type=str(task.input.get("type") or task.input.get("permission_type") or "user").strip(),
                email_address=str(task.input.get("email_address") or task.input.get("email") or "").strip(),
                domain=str(task.input.get("domain") or "").strip(),
                send_notification_email=self._bool(task.input.get("send_notification_email"), True),
            )
            output = {
                "status": "completed",
                "operation": operation,
                "spreadsheet_id": spreadsheet_id,
                "url": spreadsheet_url(spreadsheet_id),
                "account": self._account_info(),
                "permission": permission,
            }
        else:
            raise ValueError(f"Unsupported Drive operation: {operation}")
        await self._step(2, "completed", f"Completed {operation}.")
        await self._step(3, "completed", "Returned Drive operation result.")
        return AgentResult(status="completed", output=output, artifacts=[])

    async def _format_header_row(
        self,
        client: GoogleSheetsClient,
        spreadsheet_id: str,
        navigator: SheetNavigator,
        *,
        sheet_name: str,
        background_color: str = "#E8F0FE",
    ) -> dict[str, Any]:
        sheet_id = navigator.sheet_id_for(sheet_name)
        if sheet_id is None:
            raise ValueError(f"Sheet tab not found: {sheet_name}")
        color = self._hex_color(background_color)
        requests = [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color,
                            "textFormat": {"bold": True},
                            "horizontalAlignment": "CENTER",
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                }
            },
        ]
        return await client.batch_update(spreadsheet_id, requests)

    async def _format_range(
        self,
        client: GoogleSheetsClient,
        spreadsheet_id: str,
        navigator: SheetNavigator,
        *,
        range_name: str,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        cell_format, field_names = self._cell_format_from_input(input_data)
        if not field_names:
            raise ValueError("At least one formatting field is required for format_range.")
        requests = [
            {
                "repeatCell": {
                    "range": navigator.grid_range(range_name),
                    "cell": {"userEnteredFormat": cell_format},
                    "fields": ",".join(f"userEnteredFormat.{field_name}" for field_name in field_names),
                }
            }
        ]
        return await client.batch_update(spreadsheet_id, requests)

    async def _clear_formatting(
        self,
        client: GoogleSheetsClient,
        spreadsheet_id: str,
        navigator: SheetNavigator,
        *,
        range_name: str,
    ) -> dict[str, Any]:
        requests = [
            {
                "repeatCell": {
                    "range": navigator.grid_range(range_name),
                    "cell": {"userEnteredFormat": {}},
                    "fields": "userEnteredFormat",
                }
            }
        ]
        return await client.batch_update(spreadsheet_id, requests)

    async def _set_borders(
        self,
        client: GoogleSheetsClient,
        spreadsheet_id: str,
        navigator: SheetNavigator,
        *,
        range_name: str,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        raw_border_style = input_data.get("border_style")
        if raw_border_style is None and not isinstance(input_data.get("style"), dict):
            raw_border_style = input_data.get("style")
        border_style = self._border_style(str(raw_border_style or "SOLID"))
        width = self._bounded_int(input_data.get("border_width") or input_data.get("width"), 1, 1, 10)
        border: dict[str, Any] = {"style": border_style}
        if border_style != "NONE":
            border["width"] = width
            border["color"] = self._hex_color(str(input_data.get("border_color") or input_data.get("color") or "#D0D7DE"))
        sides = self._border_sides(input_data.get("sides") or input_data.get("border_sides") or "all")
        requests = [{"updateBorders": {"range": navigator.grid_range(range_name), **{side: border for side in sides}}}]
        return await client.batch_update(spreadsheet_id, requests)

    async def _auto_resize_dimension(
        self,
        client: GoogleSheetsClient,
        spreadsheet_id: str,
        navigator: SheetNavigator,
        *,
        range_name: str,
        dimension: str,
    ) -> dict[str, Any]:
        requests = [
            {
                "autoResizeDimensions": {
                    "dimensions": self._dimension_range(navigator.grid_range(range_name), dimension)
                }
            }
        ]
        return await client.batch_update(spreadsheet_id, requests)

    async def _resize_dimension(
        self,
        client: GoogleSheetsClient,
        spreadsheet_id: str,
        navigator: SheetNavigator,
        *,
        range_name: str,
        dimension: str,
        pixel_size: int,
    ) -> dict[str, Any]:
        requests = [
            {
                "updateDimensionProperties": {
                    "range": self._dimension_range(navigator.grid_range(range_name), dimension),
                    "properties": {"pixelSize": pixel_size},
                    "fields": "pixelSize",
                }
            }
        ]
        return await client.batch_update(spreadsheet_id, requests)

    async def _freeze_panes(
        self,
        client: GoogleSheetsClient,
        spreadsheet_id: str,
        navigator: SheetNavigator,
        *,
        sheet_name: str,
        frozen_row_count: int,
        frozen_column_count: int,
    ) -> dict[str, Any]:
        sheet_id = navigator.sheet_id_for(sheet_name)
        if sheet_id is None:
            raise ValueError(f"Sheet tab not found: {sheet_name}")
        requests = [
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {
                            "frozenRowCount": frozen_row_count,
                            "frozenColumnCount": frozen_column_count,
                        },
                    },
                    "fields": "gridProperties(frozenRowCount,frozenColumnCount)",
                }
            }
        ]
        return await client.batch_update(spreadsheet_id, requests)

    async def _merge_cells(
        self,
        client: GoogleSheetsClient,
        spreadsheet_id: str,
        navigator: SheetNavigator,
        *,
        range_name: str,
        unmerge: bool,
        merge_type: str,
    ) -> dict[str, Any]:
        grid_range = navigator.grid_range(range_name)
        if grid_range["endRowIndex"] - grid_range["startRowIndex"] == 1 and grid_range["endColumnIndex"] - grid_range["startColumnIndex"] == 1:
            raise ValueError("merge_cells/unmerge_cells requires a range larger than one cell.")
        if unmerge:
            requests = [{"unmergeCells": {"range": grid_range}}]
        else:
            normalized_type = self._merge_type(merge_type)
            requests = [{"mergeCells": {"range": grid_range, "mergeType": normalized_type}}]
        return await client.batch_update(spreadsheet_id, requests)

    async def _add_banding(
        self,
        client: GoogleSheetsClient,
        spreadsheet_id: str,
        navigator: SheetNavigator,
        *,
        range_name: str,
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        requests = [
            {
                "addBanding": {
                    "bandedRange": {
                        "range": navigator.grid_range(range_name),
                        "rowProperties": {
                            "headerColor": self._hex_color(str(input_data.get("header_color") or "#E8F0FE")),
                            "firstBandColor": self._hex_color(str(input_data.get("first_band_color") or "#FFFFFF")),
                            "secondBandColor": self._hex_color(str(input_data.get("second_band_color") or "#F6F8FA")),
                        },
                    }
                }
            }
        ]
        return await client.batch_update(spreadsheet_id, requests)

    async def _apply_internal_plan_if_needed(self, task: TaskEnvelope, *, purpose: str) -> TaskEnvelope:
        if not self._should_use_internal_planner(task):
            return task
        try:
            payload = await self._planner_payload(task, purpose=purpose)
            plan = await invoke_google_sheets_planner_llm(
                cfg=self.config,
                http_client=self._http_client,
                user_payload=payload,
                task_context=self._task_context(task),
            )
        except Exception as exc:
            logger.warning("google_sheets_agent.internal_planner_failed task_id=%s error=%s", task.task_id, exc)
            return task

        if self._bool(plan.get("needs_clarification"), False):
            question = str(plan.get("clarifying_question") or "").strip()
            raise ValueError(f"Clarification needed: {question}" if question else "Clarification needed before Sheets operation.")
        if self._bool(plan.get("needs_approval"), False) and not self._bool(task.input.get("approval_confirmed"), False):
            reason = str(plan.get("approval_reason") or "This Google Sheets operation requires explicit user approval.").strip()
            raise ApprovalRequiredError(reason)

        params = plan.get("params") if isinstance(plan.get("params"), dict) else {}
        merged = dict(task.input)
        for key, value in params.items():
            if value in (None, "", [], {}):
                continue
            if key not in merged or merged.get(key) in (None, "", [], {}):
                merged[key] = value
        if plan.get("operation") and str(merged.get("operation") or "").strip() in {"", "auto"}:
            merged["operation"] = plan.get("operation")
        if plan.get("intent") and str(plan.get("intent")) in {
            self.RESOLVE_RESOURCE,
            self.CREATE,
            self.READ,
            self.EDIT,
        }:
            # The current task intent remains authoritative for routing. The
            # planner intent is carried only for audit.
            merged["planner_intent"] = plan.get("intent")
        merged["internal_plan"] = {
            "operation": plan.get("operation"),
            "confidence": plan.get("confidence"),
            "reasoning": plan.get("reasoning"),
        }
        return task.model_copy(update={"input": merged})

    async def _planner_payload(self, task: TaskEnvelope, *, purpose: str) -> dict[str, Any]:
        context: dict[str, Any] = {}
        spreadsheet_id = self._spreadsheet_id_from_input(task.input)
        if spreadsheet_id and self.auth and self.auth.get("access_token"):
            try:
                structure = await self._client().get_spreadsheet(spreadsheet_id)
                context["structure"] = SheetNavigator(structure).summary()
            except Exception as exc:
                context["structure_error"] = str(exc)[:250]
        return {
            "purpose": purpose,
            "intent": task.intent,
            "input": task.input,
            "account": self._account_info(),
            "context": context,
        }

    def _should_use_internal_planner(self, task: TaskEnvelope) -> bool:
        if not self.config.enable_internal_llm:
            return False
        if not self.config.internal_llm_api_key or not self.config.internal_llm_base_url:
            return False
        operation = str(task.input.get("operation") or "").strip()
        if operation == "auto":
            return True
        high_level_keys = ("query", "user_request", "natural_language_request", "instructions", "goal")
        return any(str(task.input.get(key) or "").strip() for key in high_level_keys) and not operation

    def _sheet_specs_from_input(self, input_data: dict[str, Any]) -> list[dict[str, Any]]:
        raw_sheets = input_data.get("sheets")
        specs: list[dict[str, Any]] = []
        if isinstance(raw_sheets, list):
            for index, item in enumerate(raw_sheets):
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or item.get("sheet_name") or f"Sheet{index + 1}").strip() or f"Sheet{index + 1}"
                rows = normalize_rows(item.get("values") or item.get("rows") or item.get("data"))
                specs.append(
                    {
                        "title": title,
                        "range": str(item.get("range") or "A1").strip() or "A1",
                        "values": rows,
                        "has_header": self._bool(item.get("has_header"), bool(rows)),
                    }
                )
        if specs:
            return specs
        rows = rows_from_input(input_data)
        return [
            {
                "title": str(input_data.get("sheet_name") or "Sheet1").strip() or "Sheet1",
                "range": str(input_data.get("range") or "A1").strip() or "A1",
                "values": rows,
                "has_header": self._bool(input_data.get("has_header"), bool(rows)),
            }
        ]

    def _assert_write_budget(self, rows: list[list[Any]]) -> None:
        if not rows:
            raise ValueError("values are required for this Sheets write operation.")
        cells = count_cells(rows)
        if cells > self.config.max_write_cells:
            raise ValueError(f"Write would touch {cells} cells; limit is {self.config.max_write_cells}.")

    def _assert_read_budget(self, rows: list[list[Any]]) -> None:
        cells = count_cells(rows)
        if cells > self.config.max_read_cells:
            raise ValueError(f"Read returned {cells} cells; limit is {self.config.max_read_cells}. Narrow the range.")

    def _require_share_approval(self, input_data: dict[str, Any]) -> None:
        role = str(input_data.get("role") or "reader").strip().lower()
        permission_type = str(input_data.get("type") or input_data.get("permission_type") or "user").strip().lower()
        risky = role in {"writer", "commenter"} or permission_type in {"anyone", "domain"}
        if risky and not self._bool(input_data.get("approval_confirmed"), False):
            raise ApprovalRequiredError("Sharing this sheet with elevated or broad access requires explicit approval.")

    def _record_edit(self, task: TaskEnvelope, spreadsheet_id: str, title: str, edit: dict[str, Any]) -> None:
        account = self._account_info()
        try:
            edit_id = f"gse_{hashlib.sha256(f'{task.task_id}:{time.time_ns()}'.encode()).hexdigest()[:16]}"
            with connect_sync(self.session_db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO google_sheets_edits
                    (edit_id, task_id, session_id, account_id, account_email, spreadsheet_id, title,
                     operation, range_name, request_json, before_json, after_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        edit_id,
                        task.task_id,
                        task.session_id,
                        account.get("account_id"),
                        account.get("account_email"),
                        spreadsheet_id,
                        title,
                        edit.get("operation"),
                        edit.get("range"),
                        json.dumps(edit.get("requests") or {}, ensure_ascii=False),
                        json.dumps(edit.get("before") or {}, ensure_ascii=False),
                        json.dumps(edit.get("after") or {}, ensure_ascii=False),
                        utcnow().isoformat(),
                    ],
                )
                conn.commit()
        except Exception:
            logger.debug("google_sheets_agent.record_edit_failed task_id=%s", task.task_id, exc_info=True)

    def _save_session(self, task: TaskEnvelope, output: dict[str, Any]) -> None:
        spreadsheet = output.get("spreadsheet") if isinstance(output.get("spreadsheet"), dict) else {}
        spreadsheet_id = str(
            output.get("spreadsheet_id")
            or spreadsheet.get("spreadsheet_id")
            or task.input.get("spreadsheet_id")
            or task.input.get("file_id")
            or ""
        ).strip()
        title = str(output.get("title") or spreadsheet.get("title") or "").strip()
        operation = str(output.get("operation") or task.input.get("operation") or "").strip()
        account = self._account_info()
        try:
            with connect_sync(self.session_db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO google_sheets_session_runs
                    (task_id, session_id, intent, account_id, account_email, spreadsheet_id, title,
                     operation, result_summary_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        task.task_id,
                        task.session_id,
                        task.intent,
                        account.get("account_id"),
                        account.get("account_email"),
                        spreadsheet_id,
                        title,
                        operation,
                        json.dumps(self._summary_for_output(output), ensure_ascii=False),
                        utcnow().isoformat(),
                    ],
                )
                conn.commit()
        except Exception:
            logger.debug("google_sheets_agent.save_session_failed task_id=%s", task.task_id, exc_info=True)

    async def _post_specialist_usage(self, task: TaskEnvelope, metered_call, result: AgentResult, started: float) -> None:
        if not self.config.gateway_internal_token:
            return
        operation = str(task.input.get("operation") or task.intent).strip().replace("sheets.", "")
        error_code = result.error.code if result.error else None
        try:
            event = build_usage_event(
                metered_call=metered_call,
                source_component="agent",
                source_id=self.agent_id,
                task_id=task.task_id,
                parent_task_id=task.parent_task_id,
                session_id=task.session_id,
                route="google_sheets",
                operation=f"agent.google_sheets.{operation}",
                provider="google",
                model="google-sheets-api",
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
            logger.debug("google_sheets_agent.usage_post_failed task_id=%s", task.task_id, exc_info=True)

    def _client(self) -> GoogleSheetsClient:
        return GoogleSheetsClient(self._require_auth(), timeout_sec=self.config.request_timeout_sec)

    def _require_auth(self) -> str:
        if not self.auth or not self.auth.get("access_token"):
            raise PermissionError("No Google credentials provided for Google Sheets operation.")
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
                return self._err("AUTH_ERROR", "Google Sheets credential expired. Requested refresh.", True, "retry")
            except Exception:
                logger.exception("google_sheets_agent.credential_refresh_request_failed task_id=%s", task.task_id)
        return self._err("AUTH_ERROR", "No Google credentials available for Google Sheets operation.", False, "escalate")

    def _require_spreadsheet_id(self, task: TaskEnvelope) -> str:
        spreadsheet_id = self._spreadsheet_id_from_input(task.input)
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required.")
        return spreadsheet_id

    @staticmethod
    def _spreadsheet_id_from_input(input_data: dict[str, Any]) -> str:
        resource = input_data.get("resource") if isinstance(input_data.get("resource"), dict) else {}
        return str(
            input_data.get("spreadsheet_id")
            or input_data.get("sheet_id")
            or input_data.get("file_id")
            or resource.get("spreadsheet_id")
            or resource.get("file_id")
            or ""
        ).strip()

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

    async def _create_plan(self, steps: list[str]) -> None:
        if self.step_plan is not None:
            await self.step_plan.create(steps)

    async def _step(self, step: int, status: str, note: str | None = None) -> None:
        if self.step_plan is not None:
            await self.step_plan.update(step, status, note)

    def _task_context(self, task: TaskEnvelope) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "parent_task_id": task.parent_task_id,
            "session_id": task.session_id,
            "request_id": task.input.get("request_id"),
            "channel": task.channel,
            "source": task.source,
            "source_id": task.source_id,
        }

    def _compact_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        compact = dict(payload)
        for key in ("before", "after"):
            item = compact.get(key)
            if isinstance(item, dict) and isinstance(item.get("values"), list):
                compact[key] = {
                    **item,
                    "values": values_preview(item.get("values") or []),
                    "preview_truncated": len(item.get("values") or []) > 10,
                }
        return compact

    def _summary_for_output(self, output: dict[str, Any]) -> dict[str, Any]:
        summary = {
            "status": output.get("status"),
            "operation": output.get("operation"),
            "spreadsheet_id": output.get("spreadsheet_id"),
            "title": output.get("title"),
            "url": output.get("url"),
        }
        result = output.get("result") if isinstance(output.get("result"), dict) else {}
        if result.get("range"):
            summary["range"] = result.get("range")
        return summary

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
    def _hex_color(value: str) -> dict[str, float]:
        raw = str(value or "").strip().lstrip("#")
        if len(raw) != 6:
            raw = "E8F0FE"
        try:
            r = int(raw[0:2], 16) / 255.0
            g = int(raw[2:4], 16) / 255.0
            b = int(raw[4:6], 16) / 255.0
        except ValueError:
            r, g, b = 232 / 255.0, 240 / 255.0, 254 / 255.0
        return {"red": r, "green": g, "blue": b}

    @classmethod
    def _cell_format_from_input(cls, input_data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        nested_style = input_data.get("style") if isinstance(input_data.get("style"), dict) else {}
        source = {**nested_style, **input_data}
        cell_format: dict[str, Any] = {}
        fields: list[str] = []

        if cls._present(source, "background_color", "background"):
            cell_format["backgroundColor"] = cls._hex_color(str(source.get("background_color") or source.get("background")))
            fields.append("backgroundColor")

        text_format: dict[str, Any] = {}
        if cls._present(source, "text_color", "foreground_color", "font_color"):
            text_format["foregroundColor"] = cls._hex_color(
                str(source.get("text_color") or source.get("foreground_color") or source.get("font_color"))
            )
            fields.append("textFormat.foregroundColor")
        if cls._present(source, "font_size"):
            text_format["fontSize"] = cls._positive_int(source.get("font_size"), "font_size", minimum=6, maximum=72)
            fields.append("textFormat.fontSize")
        for source_key, sheets_key in (
            ("bold", "bold"),
            ("italic", "italic"),
            ("underline", "underline"),
            ("strikethrough", "strikethrough"),
        ):
            if cls._present(source, source_key):
                text_format[sheets_key] = cls._bool(source.get(source_key), False)
                fields.append(f"textFormat.{sheets_key}")
        if text_format:
            cell_format["textFormat"] = text_format

        if cls._present(source, "horizontal_alignment", "align", "alignment"):
            cell_format["horizontalAlignment"] = cls._alignment(
                str(source.get("horizontal_alignment") or source.get("align") or source.get("alignment")),
                allowed={"LEFT", "CENTER", "RIGHT"},
                aliases={"START": "LEFT", "END": "RIGHT", "MIDDLE": "CENTER"},
                field_name="horizontal_alignment",
            )
            fields.append("horizontalAlignment")
        if cls._present(source, "vertical_alignment", "valign"):
            cell_format["verticalAlignment"] = cls._alignment(
                str(source.get("vertical_alignment") or source.get("valign")),
                allowed={"TOP", "MIDDLE", "BOTTOM"},
                aliases={"CENTER": "MIDDLE"},
                field_name="vertical_alignment",
            )
            fields.append("verticalAlignment")
        if cls._present(source, "wrap_strategy", "wrap"):
            cell_format["wrapStrategy"] = cls._alignment(
                str(source.get("wrap_strategy") or source.get("wrap")),
                allowed={"OVERFLOW_CELL", "LEGACY_WRAP", "CLIP", "WRAP"},
                aliases={"TRUE": "WRAP", "YES": "WRAP", "ON": "WRAP", "FALSE": "OVERFLOW_CELL", "NO": "OVERFLOW_CELL"},
                field_name="wrap_strategy",
            )
            fields.append("wrapStrategy")
        if cls._present(source, "number_format_type", "number_format_pattern", "number_format"):
            number_format = cls._number_format(source)
            cell_format["numberFormat"] = number_format
            fields.append("numberFormat")

        return cell_format, fields

    @classmethod
    def _number_format(cls, source: dict[str, Any]) -> dict[str, str]:
        raw_type = str(source.get("number_format_type") or source.get("number_format") or "NUMBER").strip().upper()
        aliases = {
            "MONEY": "CURRENCY",
            "DOLLAR": "CURRENCY",
            "DOLLARS": "CURRENCY",
            "PERCENTAGE": "PERCENT",
            "DATETIME": "DATE_TIME",
            "PLAIN_TEXT": "TEXT",
        }
        number_type = aliases.get(raw_type, raw_type)
        allowed = {"TEXT", "NUMBER", "PERCENT", "CURRENCY", "DATE", "TIME", "DATE_TIME", "SCIENTIFIC"}
        if number_type not in allowed:
            raise ValueError(f"Unsupported number_format_type: {raw_type}")
        result = {"type": number_type}
        pattern = str(source.get("number_format_pattern") or "").strip()
        if pattern:
            result["pattern"] = pattern
        return result

    @classmethod
    def _border_style(cls, value: str) -> str:
        normalized = str(value or "SOLID").strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {"MEDIUM": "SOLID_MEDIUM", "THICK": "SOLID_THICK", "DASH": "DASHED", "DOT": "DOTTED", "CLEAR": "NONE"}
        normalized = aliases.get(normalized, normalized)
        allowed = {"DOTTED", "DASHED", "SOLID", "SOLID_MEDIUM", "SOLID_THICK", "DOUBLE", "NONE"}
        if normalized not in allowed:
            raise ValueError(f"Unsupported border_style: {value}")
        return normalized

    @classmethod
    def _border_sides(cls, value: Any) -> list[str]:
        raw_items = value if isinstance(value, list) else str(value or "all").replace(",", " ").split()
        normalized: set[str] = set()
        aliases = {
            "all": ["top", "bottom", "left", "right", "innerHorizontal", "innerVertical"],
            "outer": ["top", "bottom", "left", "right"],
            "inner": ["innerHorizontal", "innerVertical"],
            "inside": ["innerHorizontal", "innerVertical"],
            "horizontal": ["top", "bottom", "innerHorizontal"],
            "vertical": ["left", "right", "innerVertical"],
            "top": ["top"],
            "bottom": ["bottom"],
            "left": ["left"],
            "right": ["right"],
            "innerhorizontal": ["innerHorizontal"],
            "inner_horizontal": ["innerHorizontal"],
            "innervertical": ["innerVertical"],
            "inner_vertical": ["innerVertical"],
        }
        for item in raw_items:
            key = str(item or "").strip().lower().replace("-", "_")
            for side in aliases.get(key, []):
                normalized.add(side)
        if not normalized:
            raise ValueError("Unsupported border sides. Use all, outer, inner, top, bottom, left, right, innerHorizontal, or innerVertical.")
        order = ["top", "bottom", "left", "right", "innerHorizontal", "innerVertical"]
        return [side for side in order if side in normalized]

    @classmethod
    def _dimension_from_input(cls, input_data: dict[str, Any], *, default: str) -> str:
        raw = str(input_data.get("dimension") or input_data.get("axis") or default).strip().upper()
        aliases = {"COLUMN": "COLUMNS", "COL": "COLUMNS", "COLS": "COLUMNS", "ROW": "ROWS"}
        dimension = aliases.get(raw, raw)
        if dimension not in {"ROWS", "COLUMNS"}:
            raise ValueError("dimension must be ROWS or COLUMNS.")
        return dimension

    @staticmethod
    def _dimension_range(grid_range: dict[str, int], dimension: str) -> dict[str, int | str]:
        if dimension == "ROWS":
            return {
                "sheetId": grid_range["sheetId"],
                "dimension": "ROWS",
                "startIndex": grid_range["startRowIndex"],
                "endIndex": grid_range["endRowIndex"],
            }
        return {
            "sheetId": grid_range["sheetId"],
            "dimension": "COLUMNS",
            "startIndex": grid_range["startColumnIndex"],
            "endIndex": grid_range["endColumnIndex"],
        }

    @classmethod
    def _merge_type(cls, value: str) -> str:
        normalized = str(value or "MERGE_ALL").strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {"ALL": "MERGE_ALL", "ROWS": "MERGE_ROWS", "COLUMNS": "MERGE_COLUMNS", "COLS": "MERGE_COLUMNS"}
        normalized = aliases.get(normalized, normalized)
        allowed = {"MERGE_ALL", "MERGE_ROWS", "MERGE_COLUMNS"}
        if normalized not in allowed:
            raise ValueError(f"Unsupported merge_type: {value}")
        return normalized

    @classmethod
    def _alignment(
        cls,
        value: str,
        *,
        allowed: set[str],
        aliases: dict[str, str],
        field_name: str,
    ) -> str:
        normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        normalized = aliases.get(normalized, normalized)
        if normalized not in allowed:
            raise ValueError(f"Unsupported {field_name}: {value}")
        return normalized

    @staticmethod
    def _positive_int(value: Any, field_name: str, *, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer.") from exc
        if parsed < minimum or parsed > maximum:
            raise ValueError(f"{field_name} must be between {minimum} and {maximum}.")
        return parsed

    @staticmethod
    def _present(source: dict[str, Any], *keys: str) -> bool:
        return any(key in source and source.get(key) is not None and str(source.get(key)).strip() != "" for key in keys)

    @staticmethod
    def _http_status_error_detail(exc: httpx.HTTPStatusError) -> str:
        try:
            payload = exc.response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
            message = str(error.get("message") or "").strip()
            status = str(error.get("status") or "").strip()
            code = error.get("code") or exc.response.status_code
            if message:
                prefix = f"{status} " if status else ""
                return f"Google Sheets/Drive API error {code}: {prefix}{message}"
        text = (exc.response.text or "").strip()
        if text:
            return f"Google Sheets/Drive API error {exc.response.status_code}: {text[:1000]}"
        return f"Google Sheets/Drive API error: {exc.response.status_code}"

    @staticmethod
    def _err(code: str, message: str, retryable: bool, next_action: str) -> AgentResult:
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(code=code, message=message, retryable=retryable, next_action=next_action),
        )
