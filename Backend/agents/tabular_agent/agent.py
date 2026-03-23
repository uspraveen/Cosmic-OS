from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb

from shared import infer_tabular_mime_from_extension, is_supported_tabular_artifact, validate_safe_sheet_id
from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope, TaskInProgress
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, BACKEND_ROOT, TabularAgentConfig
from .internal_llm import maybe_enrich_preview_summary
from .internal_workflow import run_tabular_reason_workbook
from .tabular_usage import log_tabular_specialist_operation, monotonic_ms_since
from .workbook_bundle import (
    append_created_sheet_to_bundle,
    logical_bundle_paths,
    parse_csv_tsv,
    parse_xlsb,
    parse_xlsx,
    persist_parse_outcome,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS tabular_session_runs (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tabular_bundle_created
ON tabular_session_runs (bundle_id, created_at DESC);
"""


class TabularAgentError(RuntimeError):
    def __init__(self, *, code: str, message: str, retryable: bool, next_action: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action


class TabularAgent(AgentRuntime):
    PARSE_BUNDLE = "tabular.parse_bundle"
    BROWSE = "tabular.browse_workbook"
    SCHEMA = "tabular.schema_sheet"
    PREVIEW = "tabular.preview_sheet"
    QUERY = "tabular.query_workbook"
    EXPORT = "tabular.export_result"
    CREATE_SHEET = "tabular.create_sheet"
    REASON_WORKBOOK = "tabular.reason_workbook"

    def __init__(
        self,
        *,
        redis_client,
        config: TabularAgentConfig | None = None,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        http_client=None,
        agent_root: str | Path | None = None,
        artifacts_root: str | Path | None = None,
        store_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
    ) -> None:
        self.config = config or TabularAgentConfig.from_env()
        self.agent_root = (Path(agent_root).expanduser() if agent_root else AGENT_ROOT).resolve()
        self.store_root = (Path(store_root).expanduser() if store_root else self.agent_root / "store").resolve()
        self.runtime_root = (Path(runtime_root).expanduser() if runtime_root else self.agent_root / "runtime").resolve()
        self.data_root = self.store_root / "data"
        self.session_db_path = self.data_root / "tabular_session_runs.db"
        self.artifacts_root = (
            Path(artifacts_root).expanduser() if artifacts_root else BACKEND_ROOT / "runs" / "artifacts"
        ).resolve()

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
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with connect_sync(self.session_db_path) as conn:
            conn.executescript(_RUNS_SQL)
            conn.commit()

    async def execute(self, task: TaskEnvelope) -> AgentResult | TaskInProgress:
        started = time.perf_counter()
        operation = self._operation_for_intent(task.intent)
        try:
            if task.intent == self.PARSE_BUNDLE:
                result = await self._handle_parse_bundle(task)
            elif task.intent == self.BROWSE:
                result = await self._handle_browse(task)
            elif task.intent == self.SCHEMA:
                result = await self._handle_schema(task)
            elif task.intent == self.PREVIEW:
                result = await self._handle_preview(task)
            elif task.intent == self.QUERY:
                result = await self._handle_query(task)
            elif task.intent == self.EXPORT:
                result = await self._handle_export(task)
            elif task.intent == self.CREATE_SHEET:
                result = await self._handle_create_sheet(task)
            elif task.intent == self.REASON_WORKBOOK:
                result = await self._handle_reason_workbook(task)
            else:
                await self._maybe_log_usage(
                    task,
                    operation or task.intent,
                    started,
                    success=False,
                    error_code="INVALID_INPUT",
                    metadata={"reason": "unsupported_intent"},
                )
                return self._err("INVALID_INPUT", f"Unsupported intent: {task.intent}", False, "escalate")
        except TabularAgentError as exc:
            await self._maybe_log_usage(
                task,
                operation,
                started,
                success=False,
                error_code=exc.code,
                metadata={"next_action": exc.next_action},
            )
            return self._err(exc.code, exc.message, exc.retryable, exc.next_action)
        except Exception as exc:
            logger.exception("tabular_agent.error task_id=%s", task.task_id)
            await self._maybe_log_usage(
                task,
                operation,
                started,
                success=False,
                error_code="INTERNAL_ERROR",
                metadata={"exception": type(exc).__name__},
            )
            return self._err("INTERNAL_ERROR", str(exc)[:500], False, "escalate")

        if not isinstance(result, TaskInProgress):
            await self._maybe_log_usage(
                task,
                operation,
                started,
                success=True,
                error_code=None,
                metadata=self._usage_metadata(task.intent, result),
            )
        return result

    def _operation_for_intent(self, intent: str) -> str:
        return {
            self.PARSE_BUNDLE: "tabular.parse_bundle",
            self.BROWSE: "tabular.browse_workbook",
            self.SCHEMA: "tabular.schema_sheet",
            self.PREVIEW: "tabular.preview_sheet",
            self.QUERY: "tabular.query_workbook",
            self.EXPORT: "tabular.export_result",
            self.CREATE_SHEET: "tabular.create_sheet",
            self.REASON_WORKBOOK: "tabular.reason_workbook",
        }.get(intent, intent)

    def _usage_metadata(self, intent: str, result: AgentResult) -> dict[str, Any]:
        out = result.output if isinstance(result.output, dict) else {}
        meta: dict[str, Any] = {"status": result.status}
        if intent == self.PARSE_BUNDLE:
            meta["workbook_count"] = out.get("workbook_count")
        elif intent == self.QUERY:
            meta["row_count"] = out.get("row_count")
            meta["truncated"] = out.get("truncated")
        elif intent == self.EXPORT:
            meta["row_count"] = out.get("row_count")
            meta["format"] = out.get("format")
        elif intent == self.CREATE_SHEET:
            meta["sheet_id"] = out.get("sheet_id")
            meta["artifact_id"] = out.get("artifact_id")
        elif intent == self.REASON_WORKBOOK:
            meta["artifact_id"] = out.get("artifact_id")
            steps = out.get("steps")
            if isinstance(steps, list):
                meta["step_count"] = len(steps)
        return meta

    async def _maybe_log_usage(
        self,
        task: TaskEnvelope,
        operation: str,
        started: float,
        *,
        success: bool,
        error_code: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not operation:
            return
        latency_ms = monotonic_ms_since(started)
        try:
            await log_tabular_specialist_operation(
                cfg=self.config,
                http_client=self._http_client,
                operation=operation,
                task=task,
                latency_ms=latency_ms,
                success=success,
                error_code=error_code,
                metadata=metadata,
            )
        except Exception:
            logger.debug("tabular_agent.usage_log_failed", exc_info=True)

    def _err(self, code: str, message: str, retryable: bool, next_action: str) -> AgentResult:
        return AgentResult(
            status="failed",
            output={},
            artifacts=[],
            error=AgentError(code=code, retryable=retryable, message=message, next_action=next_action),
        )

    async def _handle_parse_bundle(self, task: TaskEnvelope) -> AgentResult:
        artifacts = self._normalize_input_artifacts(task.input_artifacts)
        if not artifacts:
            raise TabularAgentError(
                code="INVALID_INPUT",
                message="tabular.parse_bundle requires one or more tabular artifacts.",
                retryable=False,
                next_action="revise_input",
            )
        if len(artifacts) > self.config.max_input_artifacts:
            raise TabularAgentError(
                code="INVALID_INPUT",
                message=f"At most {self.config.max_input_artifacts} tabular artifacts per task.",
                retryable=False,
                next_action="revise_input",
            )

        bundle_id = f"bundle_{uuid4().hex[:12]}"
        bundle_label = self._safe(task.input.get("bundle_label")) if isinstance(task.input, dict) else ""
        await self._emit_stage(task.task_id, "prepare", "Preparing spreadsheet bundle.")

        sem = asyncio.Semaphore(max(1, self.config.max_parallel_files))

        async def one(a: dict[str, str], index: int, total: int) -> dict[str, Any]:
            async with sem:
                await self._emit_stage(
                    task.task_id,
                    "parse_sheets",
                    f"Parsing file {index}/{total}: {a.get('filename') or a['artifact_id']}",
                    index,
                    total,
                )
                return await asyncio.to_thread(self._parse_one_artifact, task, a, bundle_id)

        results = await asyncio.gather(
            *[one(a, i, len(artifacts)) for i, a in enumerate(artifacts, start=1)],
            return_exceptions=True,
        )
        workbooks: list[dict[str, Any]] = []
        manifests: list[ArtifactManifest] = []
        for res in results:
            if isinstance(res, Exception):
                raise res
            workbooks.append(res["summary"])
            manifests.extend(res["manifests"])

        preview_excerpt = "\n".join(self._safe(w.get("preview_excerpt")) for w in workbooks if w.get("preview_excerpt"))
        llm_note = None
        if preview_excerpt and self._http_client:
            llm_note = await maybe_enrich_preview_summary(
                cfg=self.config,
                http_client=self._http_client,
                preview_excerpt=preview_excerpt,
                task_id=task.task_id,
                session_id=task.session_id,
                request_id=self._safe(task.input.get("request_id")) if isinstance(task.input, dict) else None,
                source=task.source,
                source_id=task.source_id,
                channel=task.channel,
            )

        output = {
            "response": f"Parsed {len(workbooks)} spreadsheet file(s).",
            "bundle_id": bundle_id,
            "bundle_label": bundle_label or None,
            "workbook_count": len(workbooks),
            "workbooks": workbooks,
            "llm_workbook_note": llm_note,
        }
        self._record_run(task=task, bundle_id=bundle_id, summary=output)
        await self._emit_stage(task.task_id, "ready", "Tabular bundle ready.")
        return AgentResult(status="completed", output=output, artifacts=manifests, error=None)

    def _parse_one_artifact(self, task: TaskEnvelope, artifact: dict[str, str], bundle_id: str) -> dict[str, Any]:
        source = self._resolve_path(artifact)
        source = self._verify_file(source, artifact)
        ext = source.suffix.lower()
        if ext == ".xlsx":
            outcome = parse_xlsx(source, self.config)
        elif ext == ".xlsb":
            outcome = parse_xlsb(source, self.config)
        elif ext in {".csv"}:
            outcome = parse_csv_tsv(source, self.config, is_tsv=False)
        elif ext in {".tsv"}:
            outcome = parse_csv_tsv(source, self.config, is_tsv=True)
        else:
            raise TabularAgentError(
                code="UNSUPPORTED_ARTIFACT",
                message=f"Unsupported tabular extension: {ext}",
                retryable=False,
                next_action="revise_input",
            )

        bundle_root = self.artifacts_root / task.task_id / "parsed" / artifact["artifact_id"]
        persist_parse_outcome(bundle_root=bundle_root, outcome=outcome, cfg=self.config)

        handles = logical_bundle_paths(bundle_root, self.artifacts_root)

        manifest_payload = {
            "version": 1,
            "kind": "tabular_workbook_bundle",
            "bundle_id": bundle_id,
            "task_id": task.task_id,
            "session_id": task.session_id,
            "artifact_id": artifact["artifact_id"],
            "parse_status": outcome.parse_status,
            "created_at": utc_now_iso(),
            "handles": handles,
            "warnings": outcome.warnings,
        }
        (bundle_root / "manifest.json").write_text(
            json.dumps(manifest_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        digest = hashlib.sha256((bundle_root / "manifest.json").read_bytes()).hexdigest()

        skipped = len(outcome.skipped_sheets) + len(outcome.sheet_catalog.get("skipped", []) or [])
        parsed_sheets = len(outcome.sheet_catalog.get("sheets", []) or [])
        summary = {
            "artifact_id": artifact["artifact_id"],
            "filename": artifact.get("filename") or source.name,
            "parse_status": outcome.parse_status,
            "sheet_count": int(outcome.workbook_manifest.get("sheet_count") or parsed_sheets),
            "parsed_sheet_count": parsed_sheets,
            "skipped_or_degraded_sheet_count": skipped,
            "notable_tabs": [s.get("display_name") for s in outcome.sheet_catalog.get("sheets", []) if isinstance(s, dict)][:8],
            "formula_heavy_tabs": [],
            "warnings": outcome.warnings,
            "handles": handles,
            "preview_excerpt": (bundle_root / "preview.md").read_text(encoding="utf-8")[:4000],
        }

        art = ArtifactManifest(
            artifact_id=artifact["artifact_id"],
            task_id=task.task_id,
            mime="application/vnd.cosmic.tabular-bundle+json",
            sha256=digest,
            path=handles["bundle_root"],
            created_by_agent=self.agent_id,
            kind="output",
        )
        return {"summary": summary, "manifests": [art]}

    async def _emit_stage(
        self,
        task_id: str,
        stage: str,
        message: str,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {"stage": stage, "message": message}
        if current is not None:
            payload["current_file_index"] = current
        if total is not None:
            payload["total_files"] = total
        await self.emit_event(task_id, "task.progress", payload)

    def _record_run(self, *, task: TaskEnvelope, bundle_id: str, summary: dict[str, Any]) -> None:
        session_id = self._safe(task.session_id) or self._safe(task.task_list_id) or "sessionless"
        with connect_sync(self.session_db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tabular_session_runs (task_id, session_id, bundle_id, summary_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    session_id,
                    bundle_id,
                    json.dumps(summary, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                ),
            )
            conn.commit()

    def _patch_session_after_create_sheet(
        self,
        *,
        bundle_id: str,
        artifact_id: str,
        sheet_id: str,
        display_name: str,
    ) -> None:
        """Keep SQLite session summary aligned with on-disk bundle after tabular.create_sheet."""
        with connect_sync(self.session_db_path) as conn:
            row = conn.execute(
                "SELECT task_id, summary_json FROM tabular_session_runs WHERE bundle_id = ? ORDER BY created_at DESC LIMIT 1",
                (bundle_id,),
            ).fetchone()
        if row is None:
            return
        task_id = row["task_id"] if hasattr(row, "keys") and "task_id" in row.keys() else row[0]
        raw_json = row["summary_json"] if hasattr(row, "keys") and "summary_json" in row.keys() else row[1]
        summary = json.loads(str(raw_json))
        workbooks = summary.get("workbooks") if isinstance(summary.get("workbooks"), list) else []
        updated = False
        for wb in workbooks:
            if not isinstance(wb, dict):
                continue
            if self._safe(wb.get("artifact_id")) != artifact_id:
                continue
            parsed = int(wb.get("parsed_sheet_count") or 0) + 1
            wb["parsed_sheet_count"] = parsed
            base_count = int(wb.get("sheet_count") or parsed)
            wb["sheet_count"] = max(base_count, parsed)
            raw_tabs = wb.get("notable_tabs")
            tabs = [str(t) for t in raw_tabs] if isinstance(raw_tabs, list) else []
            if display_name and display_name not in tabs:
                tabs.append(display_name)
            wb["notable_tabs"] = tabs[:16]
            updated = True
            break
        if not updated:
            return
        with connect_sync(self.session_db_path) as conn:
            conn.execute(
                "UPDATE tabular_session_runs SET summary_json = ? WHERE task_id = ?",
                (json.dumps(summary, ensure_ascii=False), task_id),
            )
            conn.commit()

    def _load_bundle(self, bundle_id: str) -> dict[str, Any]:
        with connect_sync(self.session_db_path) as conn:
            row = conn.execute(
                "SELECT summary_json FROM tabular_session_runs WHERE bundle_id = ? ORDER BY created_at DESC LIMIT 1",
                (bundle_id,),
            ).fetchone()
        if row is None:
            raise TabularAgentError(
                code="MISSING_ARTIFACT",
                message=f"Unknown tabular bundle: {bundle_id}",
                retryable=False,
                next_action="escalate",
            )
        raw_json = row["summary_json"] if hasattr(row, "keys") and "summary_json" in row.keys() else row[1]
        return json.loads(str(raw_json))

    def _bundle_disk_path(self, bundle_id: str, artifact_id: str) -> Path:
        data = self._load_bundle(bundle_id)
        for wb in data.get("workbooks", []) if isinstance(data.get("workbooks"), list) else []:
            if not isinstance(wb, dict):
                continue
            if self._safe(wb.get("artifact_id")) == artifact_id:
                handles = wb.get("handles") if isinstance(wb.get("handles"), dict) else {}
                root = self._safe(handles.get("bundle_root"))
                if root:
                    return self._logical_to_path(root)
        raise TabularAgentError(
            code="INVALID_INPUT",
            message="artifact_id not found for this bundle.",
            retryable=False,
            next_action="revise_input",
        )

    def _logical_to_path(self, logical: str) -> Path:
        raw = logical.replace("\\", "/").strip("/")
        if raw.startswith("runs/artifacts/"):
            rel = raw.split("runs/artifacts/", 1)[1]
            return (self.artifacts_root / rel).resolve()
        return (BACKEND_ROOT / raw).resolve()

    async def _handle_browse(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require(self._safe(task.input.get("bundle_id")))
        data = self._load_bundle(bundle_id)
        return AgentResult(
            status="completed",
            output={
                "response": "Tabular bundle browse.",
                "bundle_id": bundle_id,
                "workbooks": data.get("workbooks"),
            },
            artifacts=[],
            error=None,
        )

    async def _handle_schema(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require(self._safe(task.input.get("bundle_id")))
        artifact_id = self._require(self._safe(task.input.get("artifact_id")))
        sheet_id = self._safe(task.input.get("sheet_id"))
        if sheet_id:
            try:
                sheet_id = validate_safe_sheet_id(sheet_id)
            except ValueError as exc:
                raise TabularAgentError(
                    code="INVALID_INPUT",
                    message=str(exc),
                    retryable=False,
                    next_action="revise_input",
                ) from exc
        root = self._bundle_disk_path(bundle_id, artifact_id)
        cat = json.loads((root / "sheet_catalog.json").read_text(encoding="utf-8"))
        sheets = [s for s in cat.get("sheets", []) if isinstance(s, dict)]
        if sheet_id:
            sheets = [s for s in sheets if self._safe(s.get("sheet_id")) == sheet_id]
        return AgentResult(
            status="completed",
            output={"bundle_id": bundle_id, "artifact_id": artifact_id, "schema": sheets},
            artifacts=[],
            error=None,
        )

    async def _handle_preview(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require(self._safe(task.input.get("bundle_id")))
        artifact_id = self._require(self._safe(task.input.get("artifact_id")))
        raw_sheet = self._require(self._safe(task.input.get("sheet_id")))
        try:
            sheet_id = validate_safe_sheet_id(raw_sheet)
        except ValueError as exc:
            raise TabularAgentError(
                code="INVALID_INPUT",
                message=str(exc),
                retryable=False,
                next_action="revise_input",
            ) from exc
        root = self._bundle_disk_path(bundle_id, artifact_id)
        path = root / "sheets" / f"{sheet_id}_preview.md"
        if not path.exists():
            raise TabularAgentError(code="INVALID_INPUT", message="Unknown sheet preview.", retryable=False, next_action="revise_input")
        text = path.read_text(encoding="utf-8")
        return AgentResult(
            status="completed",
            output={"bundle_id": bundle_id, "artifact_id": artifact_id, "sheet_id": sheet_id, "preview_md": text[:8000]},
            artifacts=[],
            error=None,
        )

    def sync_run_select(self, bundle_id: str, artifact_id: str, sql: str) -> dict[str, Any]:
        """Read-only SELECT against bundle DuckDB (shared by ``tabular.query_workbook`` and internal workflow)."""
        bundle_id = self._require(self._safe(bundle_id))
        artifact_id = self._require(self._safe(artifact_id))
        sql = self._require(self._safe(sql))
        if not re.match(r"(?is)\s*select\b", sql):
            raise TabularAgentError(
                code="INVALID_INPUT",
                message="Only SELECT queries are allowed.",
                retryable=False,
                next_action="revise_input",
            )
        root = self._bundle_disk_path(bundle_id, artifact_id)
        db_path = root / "bundle.duckdb"
        if not db_path.exists():
            raise TabularAgentError(code="MISSING_ARTIFACT", message="DuckDB bundle missing.", retryable=False, next_action="escalate")
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            df = con.execute(sql).fetchdf()
        finally:
            con.close()
        limit = self.config.max_query_result_rows
        return {
            "bundle_id": bundle_id,
            "artifact_id": artifact_id,
            "row_count": int(len(df)),
            "truncated": len(df) > limit,
            "columns": [str(c) for c in df.columns],
            "rows": df.head(limit).to_dict(orient="records"),
        }

    async def _handle_query(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require(self._safe(task.input.get("bundle_id")))
        artifact_id = self._require(self._safe(task.input.get("artifact_id")))
        sql = self._require(self._safe(task.input.get("sql")))
        data = self.sync_run_select(bundle_id, artifact_id, sql)
        return AgentResult(status="completed", output=data, artifacts=[], error=None)

    async def _handle_export(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require(self._safe(task.input.get("bundle_id")))
        artifact_id = self._require(self._safe(task.input.get("artifact_id")))
        sql = self._require(self._safe(task.input.get("sql")))
        fmt = (self._safe(task.input.get("format")) or "parquet").lower()
        if not re.match(r"(?is)\s*select\b", sql):
            raise TabularAgentError(code="INVALID_INPUT", message="Only SELECT queries are allowed.", retryable=False, next_action="revise_input")
        root = self._bundle_disk_path(bundle_id, artifact_id)
        db_path = root / "bundle.duckdb"
        export_dir = root / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_id = f"exp_{uuid4().hex[:10]}"
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            df = con.execute(sql).fetchdf()
        finally:
            con.close()
        out_path = export_dir / f"{export_id}.{ 'parquet' if fmt != 'csv' else 'csv'}"
        if fmt == "csv":
            df.to_csv(out_path, index=False)
        else:
            df.to_parquet(out_path, index=False)
        rel = str(out_path.relative_to(self.artifacts_root)).replace("\\", "/")
        return AgentResult(
            status="completed",
            output={
                "bundle_id": bundle_id,
                "artifact_id": artifact_id,
                "export_path": rel,
                "row_count": len(df),
                "format": fmt,
            },
            artifacts=[],
            error=None,
        )

    async def _handle_create_sheet(self, task: TaskEnvelope) -> AgentResult:
        bundle_id = self._require(self._safe(task.input.get("bundle_id")))
        artifact_id = self._require(self._safe(task.input.get("artifact_id")))
        raw_sheet = self._require(self._safe(task.input.get("sheet_id")))
        try:
            sheet_id = validate_safe_sheet_id(raw_sheet)
        except ValueError as exc:
            raise TabularAgentError(
                code="INVALID_INPUT",
                message=str(exc),
                retryable=False,
                next_action="revise_input",
            ) from exc
        columns = task.input.get("columns") if isinstance(task.input.get("columns"), list) else []
        display_name = self._safe(task.input.get("display_name")) or sheet_id
        root = self._bundle_disk_path(bundle_id, artifact_id)
        import pandas as pd

        df = pd.DataFrame(columns=[str(c) for c in columns])
        try:
            append_created_sheet_to_bundle(
                bundle_root=root,
                sheet_id=sheet_id,
                display_name=display_name,
                df=df,
                cfg=self.config,
            )
        except FileNotFoundError as exc:
            raise TabularAgentError(
                code="MISSING_ARTIFACT",
                message=f"Bundle metadata missing on disk: {exc}",
                retryable=False,
                next_action="escalate",
            ) from exc
        except ValueError as exc:
            raise TabularAgentError(
                code="INVALID_INPUT",
                message=str(exc),
                retryable=False,
                next_action="revise_input",
            ) from exc

        pq = root / "sheets" / f"{sheet_id}.parquet"
        db_path = root / "bundle.duckdb"
        con = duckdb.connect(str(db_path))
        try:
            view = f"s_{sheet_id}"
            con.execute(f'DROP VIEW IF EXISTS "{view}"')
            con.execute(f'CREATE VIEW "{view}" AS SELECT * FROM read_parquet(?)', [str(pq.resolve())])
        finally:
            con.close()

        self._patch_session_after_create_sheet(
            bundle_id=bundle_id,
            artifact_id=artifact_id,
            sheet_id=sheet_id,
            display_name=display_name,
        )
        return AgentResult(
            status="completed",
            output={
                "bundle_id": bundle_id,
                "artifact_id": artifact_id,
                "sheet_id": sheet_id,
                "display_name": display_name,
                "message": "Empty sheet created and bundle metadata updated.",
            },
            artifacts=[],
            error=None,
        )

    async def _handle_reason_workbook(self, task: TaskEnvelope) -> AgentResult | TaskInProgress:
        """Internal agentic path: plan → deterministic SQL or COSMIC sandbox → summarize."""
        if self._http_client is None:
            raise TabularAgentError(
                code="INTERNAL_ERROR",
                message="HTTP client is required for tabular internal reasoning (MiMo usage + Gateway).",
                retryable=True,
                next_action="retry",
            )
        out = await run_tabular_reason_workbook(
            agent=self,
            task=task,
            http_client=self._http_client,
            cfg=self.config,
        )
        if isinstance(out, dict) and bool(out.get("suspended")):
            return TaskInProgress(
                task_id=task.task_id,
                idempotency_key=task.idempotency_key,
                executing_since=datetime.now(timezone.utc),
                check_after_sec=max(5, min(30, int(getattr(self.config, "tabular_reason_clarify_wait_sec", 600.0) or 600.0))),
            )
        err = out.get("error") if isinstance(out, dict) else None
        if err:
            code = str(out.get("error_code") or err or "TABULAR_REASON_FAILED")
            msg = str(out.get("response") or out.get("error") or "tabular reasoning failed")
            return AgentResult(
                status="failed",
                output=out if isinstance(out, dict) else {},
                artifacts=[],
                error=AgentError(
                    code=code[:80],
                    retryable=False,
                    message=msg[:2000],
                    next_action="configure_tabular_mimo_or_use_sheets_tools",
                ),
            )
        return AgentResult(status="completed", output=out, artifacts=[], error=None)

    def _normalize_input_artifacts(self, raw: list[Any] | None) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            if not is_supported_tabular_artifact(item):
                continue
            aid = self._safe(item.get("artifact_id"))
            path = self._safe(item.get("path"))
            if not aid or not path:
                continue
            fn = self._safe(item.get("filename")) or Path(path).name
            ext = Path(fn).suffix.lower()
            mime = self._safe(item.get("mime") or item.get("mime_type")) or infer_tabular_mime_from_extension(ext)
            out.append(
                {
                    "artifact_id": aid,
                    "path": path,
                    "filename": fn,
                    "mime": mime,
                    "sha256": self._safe(item.get("sha256")),
                }
            )
        return out

    def _resolve_path(self, artifact: dict[str, str]) -> Path:
        raw = Path(artifact["path"]).expanduser()
        if raw.is_absolute():
            return raw.resolve()
        if len(raw.parts) >= 2 and raw.parts[0] == "runs" and raw.parts[1] == "artifacts":
            return (self.artifacts_root.parent.parent / raw).resolve()
        return (self.artifacts_root / raw).resolve()

    def _verify_file(self, path: Path, artifact: dict[str, str]) -> Path:
        if not path.is_file():
            raise TabularAgentError(
                code="MISSING_ARTIFACT",
                message=f"Artifact file not found: {artifact['artifact_id']}",
                retryable=False,
                next_action="escalate",
            )
        try:
            path.relative_to(self.artifacts_root.resolve())
        except ValueError as exc:
            raise TabularAgentError(
                code="MISSING_ARTIFACT",
                message="Artifact path escapes artifacts root.",
                retryable=False,
                next_action="escalate",
            ) from exc
        if path.stat().st_size > self.config.max_input_file_bytes:
            raise TabularAgentError(
                code="INVALID_INPUT",
                message="Spreadsheet exceeds configured size limit.",
                retryable=False,
                next_action="revise_input",
            )
        return path

    def _require(self, value: str) -> str:
        if not value:
            raise TabularAgentError(code="INVALID_INPUT", message="Missing required field.", retryable=False, next_action="revise_input")
        return value

    def _safe(self, value: Any) -> str:
        return str(value).strip() if value is not None else ""
