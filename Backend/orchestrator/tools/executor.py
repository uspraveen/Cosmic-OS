"""Tool executor for the COSMIC orchestrator agentic loop.

Executes tool calls made by Opus during the agentic loop. Each tool maps to
an internal COSMIC service or an external research provider.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4
from urllib.parse import quote

import httpx

from shared import begin_metered_call, build_model_key, build_usage_event, post_usage_event, validate_safe_sheet_id
from shared.contracts import AgentResult, TaskEnvelope, TaskInProgress
from shared.scratchpad import truncate_keeping_newest

from ..config import BACKEND_ROOT
from ..firecrawl_tool_enrichment import enrich_firecrawl_tool_result
from ..local_code_sandbox import LocalCodeSandboxSettings, run_local_code_sandbox
from ..sandbox_permissions import (
    build_permission_summary,
    build_sandbox_permission_receipt,
    capabilities_require_permission,
    normalize_requested_capabilities,
)
from .registry import get_local_tool_spec

logger = logging.getLogger(__name__)
HEARTBEAT_NOTES_HEADER = "# COSMIC Heartbeat Notes\n\n"
HEARTBEAT_NOTES_MAX_CHARS = 32000
_ARTIFACT_READ_MAX_CHARS = 200_000
_ARTIFACT_READ_TEXT_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".html",
        ".htm",
        ".json",
        ".jsonl",
        ".csv",
        ".tsv",
        ".css",
        ".js",
        ".ts",
        ".tsx",
        ".py",
        ".xml",
        ".svg",
    }
)


class ToolHTTPError(RuntimeError):
    def __init__(self, *, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    task_id: str | None = None
    request_id: str | None = None
    session_id: str | None = None
    channel: str | None = None
    source: str | None = None
    source_id: str | None = None
    parent_task: TaskEnvelope | None = None


class ToolExecutor:
    """Executes orchestrator tool calls against internal COSMIC services."""

    def __init__(
        self,
        *,
        perplexity_api_key: str = "",
        perplexity_model: str = "sonar",
        cosmic_memory_url: str = "",
        gateway_url: str = "",
        gateway_internal_token: str = "",
        usage_source_id: str = "orchestrator:tool_executor",
        artifacts_root: str | Path | None = None,
        local_code_execution_enabled: bool = True,
        local_code_execution_timeout_sec: float = 45.0,
        local_code_execution_allow_network: bool = False,
        local_code_execution_allow_pip: bool = True,
        local_code_execution_pip_timeout_sec: float = 120.0,
        local_code_execution_venv_cache_root: str | Path | None = None,
        local_code_execution_max_script_bytes: int = 256000,
        local_code_execution_max_files: int = 12,
        local_code_execution_max_file_bytes: int = 25 * 1024 * 1024,
        agent_dispatcher: Callable[..., Awaitable[AgentResult | TaskInProgress]] | None = None,
        agent_catalog_searcher: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        client: httpx.AsyncClient | None = None,
        heartbeat_notes_path: str | Path | None = None,
    ) -> None:
        self.perplexity_api_key = perplexity_api_key.strip()
        self.perplexity_model = perplexity_model.strip() or "sonar"
        self.cosmic_memory_url = cosmic_memory_url.rstrip("/") if cosmic_memory_url else ""
        self.gateway_url = gateway_url.rstrip("/") if gateway_url else ""
        self.gateway_internal_token = gateway_internal_token.strip()
        self.usage_source_id = usage_source_id.strip() or "orchestrator:tool_executor"
        self.artifacts_root = Path(artifacts_root).expanduser() if artifacts_root else None
        self.heartbeat_notes_path = (
            Path(heartbeat_notes_path).expanduser()
            if heartbeat_notes_path
            else Path(__file__).resolve().parents[2]
            / "agents"
            / "orchestrator"
            / "store"
            / "heartbeat_notes.md"
        )
        self.local_code_settings = LocalCodeSandboxSettings(
            enabled=bool(local_code_execution_enabled),
            timeout_sec=float(local_code_execution_timeout_sec or 45.0),
            allow_network=bool(local_code_execution_allow_network),
            allow_pip=bool(local_code_execution_allow_pip),
            pip_timeout_sec=float(local_code_execution_pip_timeout_sec or 120.0),
            venv_cache_root=Path(local_code_execution_venv_cache_root).expanduser()
            if local_code_execution_venv_cache_root
            else None,
            max_script_bytes=int(local_code_execution_max_script_bytes or 256000),
            max_files=int(local_code_execution_max_files or 12),
            max_file_bytes=int(local_code_execution_max_file_bytes or 25 * 1024 * 1024),
        )
        self._agent_dispatcher = agent_dispatcher
        self._agent_catalog_searcher = agent_catalog_searcher
        timeout = httpx.Timeout(30.0, connect=10.0)
        self._client = client or httpx.AsyncClient(timeout=timeout, http2=True)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> str:
        """Execute a tool call and return the result as a JSON string."""
        started_at = time.perf_counter()
        try:
            result = await self._dispatch(tool_name, tool_input, context=context)
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info("tool.executed name=%s rtt_ms=%d", tool_name, elapsed_ms)
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.warning(
                "tool.failed name=%s rtt_ms=%d error=%s",
                tool_name, elapsed_ms, str(exc)[:200],
            )
            return json.dumps(
                {
                    "error": True,
                    "tool": tool_name,
                    "message": str(exc).strip()[:500] or "Tool execution failed.",
                }
            )

    async def _dispatch(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any] | list[Any] | str:
        spec = get_local_tool_spec(tool_name)
        if spec is None or not spec.handler_method:
            return {"error": True, "message": f"Unknown or unsupported tool: {tool_name}"}
        handler = getattr(self, spec.handler_method, None)
        if handler is None:
            return {"error": True, "message": f"Tool handler is not implemented: {tool_name}"}
        return await handler(tool_input, context=context)

    # ── Perplexity Research ──────────────────────────────────────

    async def _perplexity_research(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return {"error": True, "message": "query is required"}
        if not self.perplexity_api_key:
            return {"error": True, "message": "Web search is not configured (no Perplexity API key)."}

        metered_call = begin_metered_call(prefix="call")
        response = await self._client.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {self.perplexity_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.perplexity_model,
                "messages": [
                    {"role": "system", "content": "Be precise and informative. Cite sources when possible."},
                    {"role": "user", "content": query},
                ],
                "max_tokens": 1500,
            },
        )
        if response.status_code >= 400:
            body = response.text[:300]
            await self._post_usage_event(
                build_usage_event(
                    metered_call=metered_call,
                    source_component="orchestrator",
                    source_id=self.usage_source_id,
                    task_id=context.task_id if context else None,
                    session_id=context.session_id if context else None,
                    route="opus",
                    operation="orchestrator.perplexity_research",
                    model_key=build_model_key("perplexity", self.perplexity_model),
                    request_id=context.request_id if context else None,
                    provider_request_id=(
                        response.headers.get("x-request-id")
                        or response.headers.get("request-id")
                        or response.headers.get("x-perplexity-request-id")
                        or None
                    ),
                    raw_usage=response.json().get("usage") if response.headers.get("content-type", "").startswith("application/json") else None,
                    success=False,
                    error_code=f"HTTP_{response.status_code}",
                    metadata_json={
                        "tool": "perplexity_research",
                        "status_code": response.status_code,
                    },
                )
            )
            return {"error": True, "message": f"Perplexity API error (status={response.status_code}): {body}"}

        payload = response.json()
        await self._post_usage_event(
            build_usage_event(
                metered_call=metered_call,
                source_component="orchestrator",
                source_id=self.usage_source_id,
                task_id=context.task_id if context else None,
                session_id=context.session_id if context else None,
                route="opus",
                operation="orchestrator.perplexity_research",
                model_key=build_model_key("perplexity", self.perplexity_model),
                request_id=context.request_id if context else None,
                provider_request_id=(
                    response.headers.get("x-request-id")
                    or response.headers.get("request-id")
                    or response.headers.get("x-perplexity-request-id")
                    or None
                ),
                raw_usage=payload.get("usage"),
                success=True,
                metadata_json={
                    "tool": "perplexity_research",
                },
            )
        )
        choices = payload.get("choices") or []
        if not choices:
            return {"error": True, "message": "No results from web search."}

        answer = str(choices[0].get("message", {}).get("content") or "").strip()
        citations = payload.get("citations") or []
        result: dict[str, Any] = {"answer": answer}
        if citations:
            result["citations"] = citations[:10]
        return result

    async def _post_usage_event(self, event) -> None:
        try:
            posted = await post_usage_event(
                client=self._client,
                gateway_url=self.gateway_url,
                internal_token=self.gateway_internal_token,
                event=event,
            )
            if not posted:
                logger.warning(
                    "tool.usage_post_failed llm_call_id=%s operation=%s model=%s",
                    event.llm_call_id,
                    event.operation,
                    event.model,
                )
        except Exception:
            logger.exception(
                "tool.usage_post_exception llm_call_id=%s operation=%s model=%s",
                event.llm_call_id,
                event.operation,
                event.model,
            )

    # ── Local Code Execution ────────────────────────────────────

    async def _cosmic_code_execution(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        if self.artifacts_root is None:
            return {"error": True, "message": "Local code execution requires COSMIC_ARTIFACTS_ROOT."}
        code = str(tool_input.get("code") or "")
        description = str(tool_input.get("description") or "").strip()
        packages_raw = tool_input.get("packages")
        packages = [str(item or "").strip() for item in packages_raw] if isinstance(packages_raw, list) else []
        timeout_value = tool_input.get("timeout_sec")
        timeout_sec: float | None = None
        if timeout_value not in (None, ""):
            try:
                timeout_sec = float(timeout_value)
            except (TypeError, ValueError):
                return {"error": True, "message": "timeout_sec must be numeric when provided."}
        task_id = self._coerce_task_id(tool_input, context) or "orchestrator"
        capabilities = normalize_requested_capabilities(tool_input.get("requested_capabilities"))
        settings = LocalCodeSandboxSettings(
            enabled=self.local_code_settings.enabled,
            timeout_sec=self.local_code_settings.timeout_sec,
            allow_network=self.local_code_settings.allow_network,
            allow_pip=self.local_code_settings.allow_pip,
            pip_timeout_sec=self.local_code_settings.pip_timeout_sec,
            venv_cache_root=self.local_code_settings.venv_cache_root,
            max_script_bytes=self.local_code_settings.max_script_bytes,
            max_files=self.local_code_settings.max_files,
            max_file_bytes=self.local_code_settings.max_file_bytes,
        )
        if capabilities_require_permission(
            capabilities,
            settings_allow_network=settings.allow_network,
        ):
            # The create call may run the sandbox inline when the session already
            # granted covering capabilities, so allow enough time for execution.
            create_timeout = max(60.0, (timeout_sec or settings.timeout_sec) + 60.0)
            permission_payload = await self._request_gateway_json(
                "POST",
                "/internal/sandbox-permissions/create",
                json_body={
                    "description": description or build_permission_summary(capabilities),
                    "network": capabilities.get("network"),
                    "host_read_paths": capabilities.get("host_read_paths"),
                    "host_write_paths": capabilities.get("host_write_paths"),
                    "allowed_hosts": capabilities.get("allowed_hosts"),
                    "code": code,
                    "packages": packages,
                    "timeout_sec": timeout_sec,
                    "request_id": context.request_id if context else None,
                    "session_id": context.session_id if context else None,
                    "task_id": task_id,
                    "channel": context.channel if context else None,
                },
                timeout=create_timeout,
            )
            if not isinstance(permission_payload, dict) or not permission_payload.get("permission_id"):
                return {
                    "error": True,
                    "message": str(
                        (permission_payload or {}).get("message")
                        or "Could not create a sandbox permission request."
                    ),
                }
            # The session already had a covering grant, so the gateway ran the
            # sandbox immediately. Return the result inline so the model can keep
            # working in the same turn instead of stopping for another approval.
            if permission_payload.get("auto_approved") and isinstance(
                permission_payload.get("result"), dict
            ):
                return permission_payload["result"]
            permission_id = str(permission_payload["permission_id"]).strip()
            receipt = build_sandbox_permission_receipt(
                permission_id=permission_id,
                description=description or build_permission_summary(capabilities),
                capabilities=capabilities,
            )
            response = {
                "permission_required": True,
                "status": "permission_required",
                "tool": "cosmic_code_execution",
                "message": "Sandbox needs your approval before it can use the requested network or VM file access.",
                "sandbox_permission": receipt,
            }
            presentation = self._sandbox_permission_presentation_contract(
                response=response,
                context=context,
            )
            if presentation:
                response["_cosmic_ui"] = presentation
            return response

        grant_settings = LocalCodeSandboxSettings(
            enabled=settings.enabled,
            timeout_sec=settings.timeout_sec,
            allow_network=settings.allow_network or bool(capabilities.get("network")),
            allow_pip=settings.allow_pip,
            pip_timeout_sec=settings.pip_timeout_sec,
            venv_cache_root=settings.venv_cache_root,
            max_script_bytes=settings.max_script_bytes,
            max_files=settings.max_files,
            max_file_bytes=settings.max_file_bytes,
            host_read_paths=tuple(capabilities.get("host_read_paths") or ()),
            host_write_paths=tuple(capabilities.get("host_write_paths") or ()),
            allowed_hosts=tuple(capabilities.get("allowed_hosts") or ()),
        )
        return run_local_code_sandbox(
            code=code,
            artifacts_root=self.artifacts_root,
            task_id=task_id,
            description=description,
            packages=packages,
            timeout_sec=timeout_sec,
            settings=grant_settings,
        )

    @staticmethod
    def _sandbox_permission_presentation_contract(
        *,
        response: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> dict[str, Any] | None:
        channel = str(context.channel if context else "").strip().lower()
        channel_platform = channel.split(":", 1)[0]
        if channel_platform not in {"desktop", "mobile"}:
            return None
        permission = response.get("sandbox_permission")
        if not isinstance(permission, dict):
            return None
        permission_id = str(permission.get("permission_id") or "").strip()
        if not permission_id:
            return None
        return {
            "version": 1,
            "render": "trusted_inline_block",
            "block_type": "sandbox_permission_request",
            "covers": [
                "permission summary",
                "requested network access",
                "requested VM file paths",
                "approval actions",
            ],
            "response_mode": "brief_acknowledgement",
            "instruction": (
                "The client will render a sandbox permission card with Allow/Deny controls beside your final response. "
                "Briefly explain why the sandbox needs access and wait for the user to approve or deny. "
                "Do not claim the sandbox already ran until approval completes."
            ),
        }

    # ── Specialist Agents ────────────────────────────────────────

    async def _agent_catalog_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        if self._agent_catalog_searcher is None:
            return {"error": True, "message": "Agent catalog search is not configured in this orchestrator runtime."}
        query = str(tool_input.get("query") or "").strip()
        limit = min(max(1, self._coerce_int(tool_input.get("limit"), 5)), 20)
        require_healthy = self._coerce_bool(tool_input.get("require_healthy"), default=True)
        return await self._agent_catalog_searcher(
            query=query,
            limit=limit,
            require_healthy=require_healthy,
        )

    async def _delegate_to_agent(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        intent = str(tool_input.get("intent") or "").strip()
        if not intent:
            return {"error": True, "message": "intent is required"}
        payload = tool_input.get("input")
        if not isinstance(payload, dict):
            return {"error": True, "message": "input must be an object"}
        payload = dict(payload)
        preferred_agent_id = str(tool_input.get("agent_id") or "").strip() or None
        wait_timeout_value = tool_input.get("wait_timeout_sec")
        wait_timeout_sec: float | None = None
        if wait_timeout_value not in (None, ""):
            try:
                wait_timeout_sec = max(1.0, float(wait_timeout_value))
            except (TypeError, ValueError):
                return {"error": True, "message": "wait_timeout_sec must be a number when provided"}
        input_artifacts = await self._resolve_delegate_input_artifacts(tool_input, context=context)
        if isinstance(input_artifacts, dict) and input_artifacts.get("error"):
            return input_artifacts
        if intent == "alpha.execute":
            input_artifacts = self._merge_artifact_descriptors(
                input_artifacts or [],
                context.parent_task.input_artifacts if context and context.parent_task else [],
            )
            input_artifacts = self._merge_artifact_descriptors(
                input_artifacts or [],
                self._alpha_parsed_bundle_artifacts(input_artifacts or []),
            )
            payload, externalized = self._externalize_alpha_payload(payload, context=context)
            if externalized:
                input_artifacts = self._merge_artifact_descriptors(input_artifacts or [], externalized)
        response = await self._dispatch_specialist_agent(
            intent=intent,
            payload=payload,
            context=context,
            agent_id=preferred_agent_id,
            wait_timeout_sec=wait_timeout_sec,
            input_artifacts=input_artifacts,
        )
        if "delegation" not in response:
            response["delegation"] = {
                "intent": intent,
                "agent_id": preferred_agent_id,
            }
        return response

    def _merge_artifact_descriptors(
        self,
        first: list[dict[str, Any]],
        second: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in [*first, *second]:
            if not isinstance(item, dict):
                continue
            artifact_id = str(item.get("artifact_id") or "").strip()
            path = str(item.get("path") or "").strip()
            key = (artifact_id, path)
            if not any(key) or key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _alpha_parsed_bundle_artifacts(
        self,
        input_artifacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Expose parsed document bundle files to Alpha as normal input artifacts.

        The orchestrator often knows a parsed `bundle_id` through attachment
        metadata, but Alpha needs concrete files in its workspace. Deriving
        artifacts from the docs-parser `parsed_summary.paths` keeps large parsed
        markdown/JSON out of the model payload while preserving direct CLI access.
        """

        existing_paths = {
            str(item.get("path") or "").strip()
            for item in input_artifacts
            if isinstance(item, dict) and str(item.get("path") or "").strip()
        }
        derived: list[dict[str, Any]] = []
        seen_paths = set(existing_paths)
        for artifact in input_artifacts:
            if not isinstance(artifact, dict):
                continue
            parsed_summary = (
                artifact.get("parsed_summary")
                if isinstance(artifact.get("parsed_summary"), dict)
                else {}
            )
            paths = parsed_summary.get("paths") if isinstance(parsed_summary.get("paths"), dict) else {}
            if not paths:
                continue
            source_artifact_id = str(artifact.get("artifact_id") or "").strip()
            parse_bundle_id = (
                str(artifact.get("parse_bundle_id") or "").strip()
                or str(parsed_summary.get("bundle_id") or "").strip()
            )
            doc_id = str(artifact.get("doc_id") or parsed_summary.get("doc_id") or "").strip()
            source_filename = str(artifact.get("filename") or parsed_summary.get("filename") or "").strip()
            for path_key, raw_path in paths.items():
                path = str(raw_path or "").strip()
                if not path or path in seen_paths:
                    continue
                seen_paths.add(path)
                filename = Path(path).name or f"{path_key}.bin"
                artifact_id = self._safe_alpha_parsed_artifact_id(
                    source_artifact_id=source_artifact_id,
                    parse_bundle_id=parse_bundle_id,
                    path_key=str(path_key),
                    path=path,
                )
                derived.append(
                    {
                        "artifact_id": artifact_id,
                        "kind": "parsed_document_bundle",
                        "audience": "supporting",
                        "mime": self._guess_parsed_bundle_mime(path=path, path_key=str(path_key)),
                        "mime_type": self._guess_parsed_bundle_mime(path=path, path_key=str(path_key)),
                        "filename": filename,
                        "path": path,
                        "parse_bundle_id": parse_bundle_id,
                        "doc_id": doc_id,
                        "source_artifact_id": source_artifact_id,
                        "source_filename": source_filename,
                        "bundle_path_key": str(path_key),
                        "caption": "Parsed document bundle file staged for Alpha workspace access.",
                    }
                )
        return derived

    def _safe_alpha_parsed_artifact_id(
        self,
        *,
        source_artifact_id: str,
        parse_bundle_id: str,
        path_key: str,
        path: str,
    ) -> str:
        base = source_artifact_id or parse_bundle_id or "parsed_bundle"
        digest = hashlib.sha256(f"{base}:{path_key}:{path}".encode("utf-8")).hexdigest()[:12]
        safe_base = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in base).strip("_")
        safe_key = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in path_key).strip("_")
        return f"art_alpha_parsed_{(safe_base or 'bundle')[:48]}_{(safe_key or 'file')[:32]}_{digest}"

    def _guess_parsed_bundle_mime(self, *, path: str, path_key: str) -> str:
        normalized_key = path_key.lower().strip()
        if normalized_key.endswith("_md") or normalized_key in {"document_md", "markdown"}:
            return "text/markdown"
        if normalized_key.endswith("_json") or normalized_key in {"document_json", "chunk_index", "manifest"}:
            return "application/json"
        guessed = mimetypes.guess_type(Path(path).name)[0]
        return guessed or "application/octet-stream"

    def _externalize_alpha_payload(
        self,
        payload: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Move very large Alpha input strings into files.

        Alpha is a workspace/CLI operator, so large documents, parsed markdown,
        generated HTML, logs, and other bulky context should travel as files the
        CLI can inspect. Keeping the structured payload compact avoids Redis,
        model-tool, and shell argv pressure without constraining the model's
        autonomy.
        """

        if self.artifacts_root is None:
            return payload, []
        max_inline_chars = 8000
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        if len(payload_json) <= 30000 and not self._payload_has_large_string(payload, max_inline_chars):
            return payload, []

        root_task_id = str(context.task_id if context and context.task_id else "alpha").strip() or "alpha"
        target_dir = self.artifacts_root / "alpha_handoffs" / root_task_id / uuid4().hex[:12]
        artifacts: list[dict[str, Any]] = []

        def externalize_value(value: Any, path_parts: list[str]) -> Any:
            if isinstance(value, str) and len(value) > max_inline_chars:
                filename = self._safe_handoff_filename(path_parts)
                target_dir.mkdir(parents=True, exist_ok=True)
                file_path = target_dir / filename
                file_path.write_text(value, encoding="utf-8")
                digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
                artifact_id = f"art_alpha_input_{digest[:16]}"
                artifacts.append(
                    {
                        "artifact_id": artifact_id,
                        "kind": "input",
                        "audience": "supporting",
                        "mime": "text/markdown" if filename.endswith(".md") else "text/plain",
                        "filename": filename,
                        "path": str(file_path),
                        "sha256": digest,
                        "size_bytes": len(value.encode("utf-8")),
                        "caption": "Externalized Alpha input context from a large delegation payload.",
                    }
                )
                return (
                    f"[Large Alpha input moved to artifact {artifact_id}: {file_path}. "
                    "Alpha must inspect this file directly instead of relying on inline text.]"
                )
            if isinstance(value, dict):
                return {
                    str(key): externalize_value(item, [*path_parts, str(key)])
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [
                    externalize_value(item, [*path_parts, str(index)])
                    for index, item in enumerate(value)
                ]
            return value

        return externalize_value(payload, ["alpha_input"]), artifacts

    def _payload_has_large_string(self, value: Any, limit: int) -> bool:
        if isinstance(value, str):
            return len(value) > limit
        if isinstance(value, dict):
            return any(self._payload_has_large_string(item, limit) for item in value.values())
        if isinstance(value, list):
            return any(self._payload_has_large_string(item, limit) for item in value)
        return False

    def _safe_handoff_filename(self, parts: list[str]) -> str:
        base = "_".join(part for part in parts if part).strip("_") or "alpha_input"
        safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in base)
        return f"{safe[:120] or 'alpha_input'}.md"

    async def _cosmics_capability_wishlist_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return {"error": True, "message": "query is required"}
        limit = min(max(1, self._coerce_int(tool_input.get("limit"), 3)), 10)
        payload = await self._request_gateway_json(
            "POST",
            "/internal/cosmics-capability-wishlist/search",
            json_body={"query": query, "limit": limit},
        )
        if payload is None:
            return {"error": True, "message": "Capability wishlist search did not return a payload."}
        return payload

    async def _artifact_lookup(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = self._coerce_session_id(tool_input, context)
        query = str(tool_input.get("query") or "").strip() or None
        all_sessions = self._coerce_bool(tool_input.get("all_sessions"), default=False)
        limit = min(max(1, self._coerce_int(tool_input.get("limit"), 5)), 12)
        payload = await self._request_gateway_json(
            "POST",
            "/internal/session/artifacts/search",
            json_body={
                "session_id": session_id,
                "query": query,
                "limit": limit,
                "all_sessions": all_sessions,
            },
        )
        if payload is None:
            return {"error": True, "message": "Artifact lookup did not return a payload."}
        return payload

    async def _artifact_redeliver(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        artifact_id = str(tool_input.get("artifact_id") or "").strip()
        if not artifact_id:
            return {"error": True, "message": "artifact_id is required"}
        session_id = self._coerce_session_id(tool_input, context)
        all_sessions = self._coerce_bool(tool_input.get("all_sessions"), default=False)
        payload = await self._request_gateway_json(
            "POST",
            "/internal/session/artifacts/resolve",
            json_body={
                "session_id": session_id,
                "artifact_ids": [artifact_id],
                "all_sessions": all_sessions,
            },
        )
        if payload is None:
            return {"error": True, "message": "Artifact resolution did not return a payload."}
        artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
        if not artifacts:
            return {
                "error": True,
                "artifact_id": artifact_id,
                "message": str(payload.get("message") or "Artifact could not be re-delivered."),
            }
        return {
            "found": True,
            "artifact_id": artifact_id,
            "count": len(artifacts),
            "message": "Previous produced file re-surfaced.",
            "artifacts": artifacts,
        }

    async def _artifact_read(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        if self.artifacts_root is None:
            return {"error": True, "message": "Artifact reads require COSMIC_ARTIFACTS_ROOT."}
        path = str(tool_input.get("path") or "").strip()
        artifact_id = str(tool_input.get("artifact_id") or "").strip() or None
        if not path:
            return {"error": True, "message": "path is required (use the logical path from a prior tool result's artifacts list)."}
        resolved = self._resolve_logical_artifact_read_path(path)
        if resolved is None:
            return {"error": True, "message": "Artifact path could not be resolved safely."}
        if not resolved.is_file():
            return {"error": True, "message": f"Artifact file not found at {path}."}
        suffix = resolved.suffix.lower()
        if suffix not in _ARTIFACT_READ_TEXT_SUFFIXES:
            return {
                "error": True,
                "message": f"artifact_read only supports text-like files; got {suffix or 'unknown'}. Use a specialist or Alpha for binary files.",
            }
        max_chars = min(max(1000, self._coerce_int(tool_input.get("max_chars"), 48_000)), _ARTIFACT_READ_MAX_CHARS)
        try:
            raw = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {"error": True, "message": "Artifact is not valid UTF-8 text."}
        except OSError as exc:
            return {"error": True, "message": f"Could not read artifact: {exc}"}
        truncated = len(raw) > max_chars
        content = raw[:max_chars] if truncated else raw
        return {
            "found": True,
            "artifact_id": artifact_id,
            "path": path,
            "filename": resolved.name,
            "mime": mimetypes.guess_type(resolved.name)[0] or "text/plain",
            "content": content,
            "content_chars": len(content),
            "full_chars": len(raw),
            "truncated": truncated,
            "message": "Loaded full artifact text." if not truncated else f"Loaded first {max_chars} characters of artifact text.",
        }

    def _resolve_logical_artifact_read_path(self, logical_path: str) -> Path | None:
        value = str(logical_path or "").strip()
        if not value or self.artifacts_root is None:
            return None
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            normalized = value.replace("\\", "/").lstrip("./")
            if normalized.startswith("runs/artifacts/"):
                relative = normalized[len("runs/artifacts/") :].strip("/")
                if not relative:
                    return None
                resolved = (self.artifacts_root / Path(relative)).resolve()
            else:
                resolved = (BACKEND_ROOT / Path(normalized)).resolve()
        root = self.artifacts_root.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved

    async def _cosmics_capability_wishlist_capture(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        title = str(tool_input.get("title") or "").strip()
        summary = str(tool_input.get("summary") or "").strip()
        if not title:
            return {"error": True, "message": "title is required"}
        if not summary:
            return {"error": True, "message": "summary is required"}
        payload: dict[str, Any] = {
            "title": title,
            "summary": summary,
            "desired_outcome": str(tool_input.get("desired_outcome") or "").strip() or None,
            "domain": str(tool_input.get("domain") or "").strip() or None,
            "tags": self._normalize_string_list(tool_input.get("tags")),
            "evidence": str(tool_input.get("evidence") or "").strip() or None,
            "source_component": "orchestrator",
            "source_id": context.source_id if context else None,
            "request_id": context.request_id if context else None,
            "session_id": context.session_id if context else None,
            "task_id": context.task_id if context else None,
            "route": "opus",
            "created_by": "cosmic/orchestrator:1.0.0",
            "metadata": self._clean_mapping(
                {
                    "task_source": context.source if context else None,
                    "channel": context.channel if context else None,
                }
            ),
        }
        gateway_payload = await self._request_gateway_json(
            "POST",
            "/internal/cosmics-capability-wishlist/capture",
            json_body=payload,
        )
        if gateway_payload is None:
            return {"error": True, "message": "Capability wishlist capture did not return a payload."}
        return gateway_payload

    async def _custom_tool_opportunity_capture(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        payload = {
            "title": str(tool_input.get("title") or "").strip(),
            "tool_type": str(tool_input.get("tool_type") or "site").strip(),
            "goal": str(tool_input.get("goal") or "").strip(),
            "reasoning": str(tool_input.get("reasoning") or "").strip(),
            "proposed_features": self._normalize_string_list(tool_input.get("proposed_features")),
            "helpful_materials": self._normalize_string_list(tool_input.get("helpful_materials")),
            "required_inputs": self._normalize_string_list(tool_input.get("required_inputs")),
            "data_sources": self._normalize_string_list(tool_input.get("data_sources")),
            "expected_value": str(tool_input.get("expected_value") or "").strip() or None,
            "confidence": tool_input.get("confidence"),
            "source_context_refs": [
                item for item in [
                    context.session_id if context else None,
                    context.task_id if context else None,
                    context.request_id if context else None,
                ] if item
            ],
            "trigger_source": context.source if context else "orchestrator",
            "created_by": "cosmic/orchestrator:1.0.0",
            "metadata": self._clean_mapping({
                "channel": context.channel if context else None,
                "source_id": context.source_id if context else None,
            }),
        }
        if not payload["title"] or not payload["goal"] or not payload["reasoning"]:
            return {"error": True, "message": "title, goal, and reasoning are required"}
        result = await self._request_gateway_json("POST", "/internal/tool-opportunities/capture", json_body=payload)
        return result or {"error": True, "message": "Tool opportunity capture did not return a payload."}

    async def _custom_tool_opportunities_list(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del tool_input, context
        result = await self._request_gateway_json("GET", "/internal/tool-opportunities")
        return result or {"error": True, "message": "Tool opportunities list did not return a payload."}

    async def _custom_tool_opportunity_update(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        opportunity_id = str(tool_input.get("opportunity_id") or "").strip()
        if not opportunity_id:
            return {"error": True, "message": "opportunity_id is required"}
        payload = self._clean_mapping({
            key: tool_input.get(key)
            for key in (
                "status", "alpha_project_id", "build_task_id", "deployment_url",
                "repo_url", "user_feedback", "declined_reason", "health_status",
                "title", "goal", "reasoning", "expected_value", "proposed_features",
                "helpful_materials", "required_inputs", "data_sources", "defer_until",
                "review_reason",
            )
        })
        payload["mutation_context"] = self._clean_mapping(
            {
                "actor": "cosmic/orchestrator:1.0.0",
                "source": context.source if context else None,
                "source_id": context.source_id if context else None,
                "request_id": context.request_id if context else None,
                "session_id": context.session_id if context else None,
                "task_id": context.task_id if context else None,
                "channel": context.channel if context else None,
            }
        )
        result = await self._request_gateway_json(
            "PATCH",
            f"/internal/tool-opportunities/{quote(opportunity_id, safe='')}",
            json_body=payload,
        )
        return result or {"error": True, "message": "Tool opportunity update did not return a payload."}

    async def _docs_browse(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        payload: dict[str, Any] = {
            "bundle_id": bundle_id,
            "index_kind": str(tool_input.get("index_kind") or "").strip() or "documents",
        }
        doc_id = str(tool_input.get("doc_id") or "").strip()
        if doc_id:
            payload["doc_id"] = doc_id
        limit = self._coerce_int(tool_input.get("limit"), 20)
        if limit > 0:
            payload["limit"] = min(max(limit, 1), 100)
        return await self._dispatch_specialist_agent(
            intent="docs.browse_bundle",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _docs_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        query = str(tool_input.get("query") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        if not query:
            return {"error": True, "message": "query is required"}
        payload: dict[str, Any] = {"bundle_id": bundle_id, "query": query}
        search_kind = str(tool_input.get("search_kind") or "").strip()
        if search_kind:
            payload["search_kind"] = search_kind
        doc_ids = self._normalize_string_list(tool_input.get("doc_ids"))
        if doc_ids:
            payload["doc_ids"] = doc_ids[:12]
        limit = self._coerce_int(tool_input.get("limit"), 5)
        if limit > 0:
            payload["limit"] = min(max(limit, 1), 12)
        return await self._dispatch_specialist_agent(
            intent="docs.search_bundle",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _docs_read(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        payload: dict[str, Any] = {"bundle_id": bundle_id}
        doc_id = str(tool_input.get("doc_id") or "").strip()
        if doc_id:
            payload["doc_id"] = doc_id
        read_kind = str(tool_input.get("read_kind") or "").strip()
        if read_kind:
            payload["read_kind"] = read_kind
        section_id = str(tool_input.get("section_id") or "").strip()
        if section_id:
            payload["section_id"] = section_id
        chunk_ids = self._normalize_string_list(tool_input.get("chunk_ids"))
        if chunk_ids:
            payload["chunk_ids"] = chunk_ids[:8]
        for key in ("start_page", "end_page", "start_slide", "end_slide", "offset_chars", "before_chars", "after_chars"):
            value = self._coerce_int(tool_input.get(key), 0)
            if value > 0:
                payload[key] = value
        anchor_id = str(tool_input.get("anchor_id") or "").strip()
        if anchor_id:
            payload["anchor_id"] = anchor_id
        max_chars = self._coerce_int(tool_input.get("max_chars"), 5000)
        if max_chars > 0:
            payload["max_chars"] = min(max(max_chars, 500), 12000)
        return await self._dispatch_specialist_agent(
            intent="docs.read_bundle",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _docs_fetch_asset(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        asset_id = str(tool_input.get("asset_id") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        if not asset_id:
            return {"error": True, "message": "asset_id is required"}
        payload: dict[str, Any] = {"bundle_id": bundle_id, "asset_id": asset_id}
        doc_id = str(tool_input.get("doc_id") or "").strip()
        if doc_id:
            payload["doc_id"] = doc_id
        max_chars = self._coerce_int(tool_input.get("max_chars"), 5000)
        if max_chars > 0:
            payload["max_chars"] = min(max(max_chars, 500), 12000)
        return await self._dispatch_specialist_agent(
            intent="docs.fetch_asset",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _docs_reinspect_asset(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        asset_id = str(tool_input.get("asset_id") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        if not asset_id:
            return {"error": True, "message": "asset_id is required"}
        payload: dict[str, Any] = {"bundle_id": bundle_id, "asset_id": asset_id}
        doc_id = str(tool_input.get("doc_id") or "").strip()
        if doc_id:
            payload["doc_id"] = doc_id
        question = str(tool_input.get("question") or "").strip()
        if question:
            payload["question"] = question
        return await self._dispatch_specialist_agent(
            intent="docs.reinspect_asset",
            payload=payload,
            context=context,
            agent_id="cosmic/docs-parser-agent:1.0.0",
            wait_timeout_sec=45.0,
        )

    async def _sheets_browse(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        if not bundle_id:
            return {"error": True, "message": "bundle_id is required"}
        return await self._dispatch_specialist_agent(
            intent="tabular.browse_workbook",
            payload={"bundle_id": bundle_id},
            context=context,
            agent_id="cosmic/tabular-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _sheets_schema(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        artifact_id = str(tool_input.get("artifact_id") or "").strip()
        if not bundle_id or not artifact_id:
            return {"error": True, "message": "bundle_id and artifact_id are required"}
        payload: dict[str, Any] = {"bundle_id": bundle_id, "artifact_id": artifact_id}
        sheet_id = str(tool_input.get("sheet_id") or "").strip()
        if sheet_id:
            try:
                payload["sheet_id"] = validate_safe_sheet_id(sheet_id)
            except ValueError as exc:
                return {"error": True, "message": str(exc)}
        return await self._dispatch_specialist_agent(
            intent="tabular.schema_sheet",
            payload=payload,
            context=context,
            agent_id="cosmic/tabular-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _sheets_preview(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        artifact_id = str(tool_input.get("artifact_id") or "").strip()
        sheet_id = str(tool_input.get("sheet_id") or "").strip()
        if not bundle_id or not artifact_id or not sheet_id:
            return {"error": True, "message": "bundle_id, artifact_id, and sheet_id are required"}
        try:
            sheet_id = validate_safe_sheet_id(sheet_id)
        except ValueError as exc:
            return {"error": True, "message": str(exc)}
        return await self._dispatch_specialist_agent(
            intent="tabular.preview_sheet",
            payload={"bundle_id": bundle_id, "artifact_id": artifact_id, "sheet_id": sheet_id},
            context=context,
            agent_id="cosmic/tabular-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _sheets_query(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        artifact_id = str(tool_input.get("artifact_id") or "").strip()
        sql = str(tool_input.get("sql") or "").strip()
        if not bundle_id or not artifact_id or not sql:
            return {"error": True, "message": "bundle_id, artifact_id, and sql are required"}
        return await self._dispatch_specialist_agent(
            intent="tabular.query_workbook",
            payload={"bundle_id": bundle_id, "artifact_id": artifact_id, "sql": sql},
            context=context,
            agent_id="cosmic/tabular-agent:1.0.0",
            wait_timeout_sec=90.0,
        )

    async def _sheets_export(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        artifact_id = str(tool_input.get("artifact_id") or "").strip()
        sql = str(tool_input.get("sql") or "").strip()
        if not bundle_id or not artifact_id or not sql:
            return {"error": True, "message": "bundle_id, artifact_id, and sql are required"}
        fmt = str(tool_input.get("format") or "parquet").strip().lower()
        payload: dict[str, Any] = {"bundle_id": bundle_id, "artifact_id": artifact_id, "sql": sql, "format": fmt}
        return await self._dispatch_specialist_agent(
            intent="tabular.export_result",
            payload=payload,
            context=context,
            agent_id="cosmic/tabular-agent:1.0.0",
            wait_timeout_sec=120.0,
        )

    async def _sheets_export_sheet(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        artifact_id = str(tool_input.get("artifact_id") or "").strip()
        sheet_id = str(tool_input.get("sheet_id") or "").strip()
        if not bundle_id or not artifact_id or not sheet_id:
            return {"error": True, "message": "bundle_id, artifact_id, and sheet_id are required"}
        try:
            sheet_id = validate_safe_sheet_id(sheet_id)
        except ValueError as exc:
            return {"error": True, "message": str(exc)}
        fmt = str(tool_input.get("format") or "csv").strip().lower()
        payload: dict[str, Any] = {
            "bundle_id": bundle_id,
            "artifact_id": artifact_id,
            "sheet_id": sheet_id,
            "format": fmt,
        }
        return await self._dispatch_specialist_agent(
            intent="tabular.export_sheet",
            payload=payload,
            context=context,
            agent_id="cosmic/tabular-agent:1.0.0",
            wait_timeout_sec=120.0,
        )

    async def _sheets_create_workbook(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        sheets = tool_input.get("sheets")
        if not isinstance(sheets, list) or not sheets:
            return {"error": True, "message": "sheets must be a non-empty array"}
        payload: dict[str, Any] = {"sheets": sheets}
        filename = str(tool_input.get("filename") or "").strip()
        if filename:
            payload["filename"] = filename
        bundle_label = str(tool_input.get("bundle_label") or "").strip()
        if bundle_label:
            payload["bundle_label"] = bundle_label
        return await self._dispatch_specialist_agent(
            intent="tabular.create_workbook",
            payload=payload,
            context=context,
            agent_id="cosmic/tabular-agent:1.0.0",
            wait_timeout_sec=90.0,
        )

    async def _sheets_create_sheet(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        artifact_id = str(tool_input.get("artifact_id") or "").strip()
        sheet_id = str(tool_input.get("sheet_id") or "").strip()
        columns = tool_input.get("columns")
        if not bundle_id or not artifact_id or not sheet_id:
            return {"error": True, "message": "bundle_id, artifact_id, and sheet_id are required"}
        try:
            sheet_id = validate_safe_sheet_id(sheet_id)
        except ValueError as exc:
            return {"error": True, "message": str(exc)}
        if not isinstance(columns, list) or len(columns) < 1:
            return {"error": True, "message": "columns must be a non-empty array of column name strings"}
        display_name = str(tool_input.get("display_name") or "").strip()
        payload: dict[str, Any] = {
            "bundle_id": bundle_id,
            "artifact_id": artifact_id,
            "sheet_id": sheet_id,
            "columns": [str(c) for c in columns],
        }
        if display_name:
            payload["display_name"] = display_name
        return await self._dispatch_specialist_agent(
            intent="tabular.create_sheet",
            payload=payload,
            context=context,
            agent_id="cosmic/tabular-agent:1.0.0",
            wait_timeout_sec=60.0,
        )

    async def _sheets_reason(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        bundle_id = str(tool_input.get("bundle_id") or "").strip()
        artifact_id = str(tool_input.get("artifact_id") or "").strip()
        goal = str(tool_input.get("goal") or "").strip()
        if not bundle_id or not artifact_id or not goal:
            return {"error": True, "message": "bundle_id, artifact_id, and goal are required"}
        payload: dict[str, Any] = {
            "bundle_id": bundle_id,
            "artifact_id": artifact_id,
            "goal": goal,
        }
        if "allow_python" in tool_input:
            payload["allow_python"] = bool(tool_input.get("allow_python"))
        return await self._dispatch_specialist_agent(
            intent="tabular.reason_workbook",
            payload=payload,
            context=context,
            agent_id="cosmic/tabular-agent:1.0.0",
            wait_timeout_sec=200.0,
        )

    async def _firecrawl_scrape(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "url": str(tool_input.get("url") or "").strip(),
        }
        formats = self._normalize_string_list(tool_input.get("formats"))
        if formats:
            payload["formats"] = formats
        for key in (
            "only_main_content",
            "wait_for_ms",
            "timeout_ms",
            "max_age_ms",
            "include_tags",
            "exclude_tags",
            "mobile",
            "proxy",
            "parsers",
            "screenshot_full_page",
        ):
            value = tool_input.get(key)
            if value not in (None, "", [], {}):
                payload[key] = value
        result = await self._dispatch_specialist_agent(
            intent="firecrawl.scrape",
            payload=payload,
            context=context,
            agent_id="cosmic/firecrawl-web-scrape-agent:1.0.0",
            wait_timeout_sec=125.0,
        )
        return enrich_firecrawl_tool_result(result) if isinstance(result, dict) else result

    async def _firecrawl_extract(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "urls": self._normalize_string_list(tool_input.get("urls")),
            "prompt": str(tool_input.get("prompt") or "").strip(),
        }
        for key in (
            "schema",
            "show_sources",
            "enable_web_search",
            "only_main_content",
            "wait_for_ms",
            "timeout_ms",
            "max_age_ms",
            "parsers",
        ):
            value = tool_input.get(key)
            if value not in (None, "", [], {}):
                payload[key] = value
        result = await self._dispatch_specialist_agent(
            intent="firecrawl.extract",
            payload=payload,
            context=context,
            agent_id="cosmic/firecrawl-web-scrape-agent:1.0.0",
            wait_timeout_sec=185.0,
        )
        return enrich_firecrawl_tool_result(result) if isinstance(result, dict) else result

    async def _firecrawl_agent(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": str(tool_input.get("prompt") or "").strip(),
        }
        urls = self._normalize_string_list(tool_input.get("urls"))
        if urls:
            payload["urls"] = urls
        schema = tool_input.get("schema")
        if isinstance(schema, dict) and schema:
            payload["schema"] = schema
        return await self._dispatch_specialist_agent(
            intent="firecrawl.agent",
            payload=payload,
            context=context,
            agent_id="cosmic/firecrawl-web-scrape-agent:1.0.0",
            wait_timeout_sec=295.0,
        )

    async def _firecrawl_recall_session(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": str(tool_input.get("session_id") or "").strip() or (context.session_id if context else ""),
        }
        limit = self._coerce_int(tool_input.get("limit"), 10)
        if limit > 0:
            payload["limit"] = min(max(limit, 1), 50)
        return await self._dispatch_specialist_agent(
            intent="firecrawl.recall_session",
            payload=payload,
            context=context,
            agent_id="cosmic/firecrawl-web-scrape-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _x_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return {"error": True, "message": "query is required"}
        payload: dict[str, Any] = {"query": query}
        analysis_goal = str(tool_input.get("analysis_goal") or "").strip()
        if analysis_goal:
            payload["analysis_goal"] = analysis_goal
        allowed_x_handles = self._normalize_string_list(tool_input.get("allowed_x_handles"))
        if allowed_x_handles:
            payload["allowed_x_handles"] = allowed_x_handles[:10]
        excluded_x_handles = self._normalize_string_list(tool_input.get("excluded_x_handles"))
        if excluded_x_handles:
            payload["excluded_x_handles"] = excluded_x_handles[:10]
        from_date = str(tool_input.get("from_date") or "").strip()
        if from_date:
            payload["from_date"] = from_date
        to_date = str(tool_input.get("to_date") or "").strip()
        if to_date:
            payload["to_date"] = to_date
        if "enable_image_understanding" in tool_input:
            payload["enable_image_understanding"] = self._coerce_bool(
                tool_input.get("enable_image_understanding"),
                default=False,
            )
        if "enable_video_understanding" in tool_input:
            payload["enable_video_understanding"] = self._coerce_bool(
                tool_input.get("enable_video_understanding"),
                default=False,
            )
        max_posts = self._coerce_int(tool_input.get("max_posts"), 30)
        if max_posts > 0:
            payload["max_posts"] = min(max(max_posts, 1), 30)
        return await self._dispatch_specialist_agent(
            intent="x.search",
            payload=payload,
            context=context,
            agent_id="cosmic/x-twitter-search-agent:1.0.0",
            wait_timeout_sec=35.0,
        )

    async def _x_recall_session(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = str(tool_input.get("session_id") or "").strip() or (context.session_id if context else "")
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        limit = self._coerce_int(tool_input.get("limit"), 10)
        return await self._dispatch_specialist_agent(
            intent="x.recall_session",
            payload={"session_id": session_id, "limit": min(max(limit, 1), 50)},
            context=context,
            agent_id="cosmic/x-twitter-search-agent:1.0.0",
            wait_timeout_sec=20.0,
        )

    # ── Memory Search / Write ────────────────────────────────────

    async def _memory_search(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        query = str(tool_input.get("query") or "").strip()
        if not query:
            return {"error": True, "message": "query is required"}

        payload: dict[str, Any] = {
            "query": query,
            "max_results": min(max(1, self._coerce_int(tool_input.get("max_results"), 5)), 20),
            "token_budget": min(max(256, self._coerce_int(tool_input.get("token_budget"), 3000)), 12000),
        }
        kinds = self._normalize_string_list(tool_input.get("kinds"))
        if kinds:
            payload["kinds"] = kinds
        seed_memory_ids = self._normalize_string_list(tool_input.get("seed_memory_ids"))
        if seed_memory_ids:
            payload["seed_memory_ids"] = seed_memory_ids
        seed_entities = self._normalize_string_list(tool_input.get("seed_entities"))
        if seed_entities:
            payload["seed_entities"] = seed_entities
        max_hops = self._coerce_int(tool_input.get("max_hops"), 2)
        if max_hops > 0:
            payload["max_hops"] = min(max_hops, 6)
        if bool(tool_input.get("include_diagnostics")):
            payload["include_diagnostics"] = True

        if self.gateway_url:
            try:
                response_payload = await self._request_gateway_json(
                    "POST",
                    "/internal/memory/active-search",
                    json_body=payload,
                )
                return self._normalize_memory_search_result(response_payload)
            except ToolHTTPError as exc:
                if exc.status_code not in {404, 405} or not self.cosmic_memory_url:
                    raise

        if self.cosmic_memory_url:
            response_payload = await self._request_cosmic_memory_json(
                "POST",
                "/v1/query/active",
                json_body=payload,
            )
            return self._normalize_memory_search_result(response_payload)

        raise RuntimeError("Memory service is not configured.")

    async def _memory_fetch(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        memory_id = str(tool_input.get("memory_id") or "").strip()
        if not memory_id:
            return {"error": True, "message": "memory_id is required"}

        gateway_path = f"/internal/memory/memories/{quote(memory_id, safe='')}"
        memory_path = f"/v1/memories/{quote(memory_id, safe='')}"

        if self.gateway_url:
            try:
                response_payload = await self._request_gateway_json(
                    "GET",
                    gateway_path,
                    allow_404=True,
                )
                if response_payload is None:
                    return {
                        "found": False,
                        "memory_id": memory_id,
                        "message": "Memory not found.",
                    }
                return self._normalize_memory_record_result(response_payload, requested_memory_id=memory_id)
            except ToolHTTPError as exc:
                if exc.status_code not in {404, 405} or not self.cosmic_memory_url:
                    raise

        if self.cosmic_memory_url:
            response_payload = await self._request_cosmic_memory_json(
                "GET",
                memory_path,
                allow_404=True,
            )
            if response_payload is None:
                return {
                    "found": False,
                    "memory_id": memory_id,
                    "message": "Memory not found.",
                }
            return self._normalize_memory_record_result(response_payload, requested_memory_id=memory_id)

        raise RuntimeError("Memory service is not configured.")

    async def _memory_write(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        content = str(tool_input.get("content") or "").strip()
        original_kind = str(tool_input.get("kind") or "agent_note").strip() or "agent_note"
        kind = self._normalize_memory_write_kind(original_kind)
        title = str(tool_input.get("title") or "").strip() or self._derive_memory_title(content)
        if not content:
            return {"error": True, "message": "content is required"}
        if kind is None:
            return {
                "error": True,
                "message": "Unsupported memory_write kind. Use user_data or agent_note. Stable always-on facts should use memory_write_core_fact.",
            }

        metadata = tool_input.get("metadata") if isinstance(tool_input.get("metadata"), dict) else {}
        payload: dict[str, Any] = {
            "content": content,
            "kind": kind,
            "title": title,
        }
        tags = self._normalize_string_list(tool_input.get("tags"))
        if tags:
            payload["tags"] = tags
        merged_metadata = self._merge_dicts(
            self._build_memory_metadata(context),
            metadata,
        )
        if merged_metadata:
            payload["metadata"] = merged_metadata
        provenance = self._build_orchestrator_provenance(context)
        if provenance:
            payload["provenance"] = provenance

        if self.gateway_url:
            try:
                response_payload = await self._request_gateway_json(
                    "POST",
                    "/internal/memory/write",
                    json_body=payload,
                )
                return {
                    "saved": True,
                    "deduplicated": bool(response_payload.get("deduplicated")),
                    "id": response_payload.get("memory_id") or response_payload.get("id"),
                    "message": (
                        f"Memory already captured: {title}"
                        if bool(response_payload.get("deduplicated"))
                        else f"Memory saved: {title}"
                    ),
                    "kind": kind,
                    "original_kind": original_kind,
                    "record": response_payload,
                }
            except ToolHTTPError as exc:
                if exc.status_code not in {404, 405} or not self.cosmic_memory_url:
                    raise

        if self.cosmic_memory_url:
            response_payload = await self._request_cosmic_memory_json(
                "POST",
                "/v1/memories",
                json_body=payload,
            )
            return {
                "saved": True,
                "deduplicated": bool(response_payload.get("deduplicated")),
                "id": response_payload.get("memory_id") or response_payload.get("id"),
                "message": (
                    f"Memory already captured: {title}"
                    if bool(response_payload.get("deduplicated"))
                    else f"Memory saved: {title}"
                ),
                "kind": kind,
                "original_kind": original_kind,
                "record": response_payload,
            }

        raise RuntimeError("Memory service is not configured.")

    async def _heartbeat_notes(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        action = str(tool_input.get("action") or "read").strip().lower() or "read"
        if action not in {"read", "append", "replace", "remove", "clear"}:
            return {
                "error": True,
                "message": "Unsupported heartbeat_notes action. Use read, append, replace, remove, or clear.",
            }

        current = self._read_heartbeat_notes_text()
        changed = False
        message = "Heartbeat notes read."
        if action == "append":
            content = self._normalize_heartbeat_notes_fragment(tool_input.get("content"))
            if not content:
                return {"error": True, "message": "content is required for append"}
            body = current.rstrip()
            current = f"{body}\n\n{content}\n" if body else f"{HEARTBEAT_NOTES_HEADER}{content}\n"
            changed = True
            message = "Heartbeat notes appended."
        elif action == "replace":
            content = self._normalize_heartbeat_notes_fragment(tool_input.get("content"))
            if not content:
                return {"error": True, "message": "content is required for replace"}
            current = content
            changed = True
            message = "Heartbeat notes replaced."
        elif action == "remove":
            match = str(tool_input.get("match") or "").strip()
            if not match:
                return {"error": True, "message": "match is required for remove"}
            if match not in current:
                return {
                    "updated": False,
                    "message": "No matching heartbeat note text found.",
                    "content": current,
                    "bytes": len(current.encode("utf-8")),
                    "path": str(self.heartbeat_notes_path),
                }
            current = current.replace(match, "").strip() + "\n"
            changed = True
            message = "Heartbeat notes removed matching text."
        elif action == "clear":
            current = HEARTBEAT_NOTES_HEADER
            changed = True
            message = "Heartbeat notes cleared."

        if changed:
            current = self._write_heartbeat_notes_text(current)
        return {
            "updated": changed,
            "message": message,
            "content": current,
            "bytes": len(current.encode("utf-8")),
            "path": str(self.heartbeat_notes_path),
        }

    async def _memory_write_core_fact(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        fact = str(tool_input.get("fact") or tool_input.get("content") or "").strip()
        title = str(tool_input.get("title") or "").strip() or self._derive_memory_title(fact)
        if not fact:
            return {"error": True, "message": "fact is required"}

        metadata = tool_input.get("metadata") if isinstance(tool_input.get("metadata"), dict) else {}
        payload: dict[str, Any] = {
            "fact": fact,
            "title": title,
            "priority": min(max(self._coerce_int(tool_input.get("priority"), 100), 0), 1000),
            "always_include": self._coerce_bool(tool_input.get("always_include"), True),
        }
        canonical_key = str(tool_input.get("canonical_key") or "").strip()
        if canonical_key:
            payload["canonical_key"] = canonical_key
        tags = self._normalize_string_list(tool_input.get("tags"))
        if tags:
            payload["tags"] = tags
        merged_metadata = self._merge_dicts(
            self._build_memory_metadata(context),
            metadata,
        )
        if merged_metadata:
            payload["metadata"] = merged_metadata
        provenance = self._build_orchestrator_provenance(context)
        if provenance:
            payload["provenance"] = provenance

        if self.gateway_url:
            try:
                response_payload = await self._request_gateway_json(
                    "POST",
                    "/internal/memory/core-facts",
                    json_body=payload,
                )
                return {
                    "saved": True,
                    "deduplicated": bool(response_payload.get("deduplicated")),
                    "id": response_payload.get("memory_id") or response_payload.get("id"),
                    "kind": "core_fact",
                    "title": title,
                    "canonical_key": canonical_key,
                    "message": (
                        f"Core fact already captured: {title}"
                        if bool(response_payload.get("deduplicated"))
                        else f"Core fact saved: {title}"
                    ),
                    "record": response_payload,
                }
            except ToolHTTPError as exc:
                if exc.status_code not in {404, 405} or not self.cosmic_memory_url:
                    raise

        if self.cosmic_memory_url:
            response_payload = await self._request_cosmic_memory_json(
                "POST",
                "/v1/core-facts",
                json_body=payload,
            )
            return {
                "saved": True,
                "deduplicated": bool(response_payload.get("deduplicated")),
                "id": response_payload.get("memory_id") or response_payload.get("id"),
                "kind": "core_fact",
                "title": title,
                "canonical_key": canonical_key,
                "message": (
                    f"Core fact already captured: {title}"
                    if bool(response_payload.get("deduplicated"))
                    else f"Core fact saved: {title}"
                ),
                "record": response_payload,
            }

        raise RuntimeError("Memory service is not configured.")

    # ── Session / Task Revisit ───────────────────────────────────

    async def _session_state(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = self._coerce_session_id(tool_input, context)
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        return await self._request_gateway_json("GET", f"/internal/session/state/{session_id}")

    async def _session_turns(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = self._coerce_session_id(tool_input, context)
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        limit = min(max(1, self._coerce_int(tool_input.get("limit"), 20)), 200)
        return await self._request_gateway_json(
            "GET",
            f"/internal/session/turns/{session_id}",
            params={"limit": limit},
        )

    async def _session_history(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = self._coerce_session_id(tool_input, context)
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        limit = min(max(1, self._coerce_int(tool_input.get("limit"), 20)), 200)
        offset = max(0, self._coerce_int(tool_input.get("offset"), 0))
        return await self._request_gateway_json(
            "GET",
            f"/internal/session/history/{session_id}",
            params={"limit": limit, "offset": offset},
        )

    async def _task_notebook(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        task_id = self._coerce_task_id(tool_input, context)
        if not task_id:
            return {"error": True, "message": "task_id is required"}
        payload = await self._request_gateway_json(
            "GET",
            f"/internal/session/task-notebook/{task_id}",
            allow_404=True,
        )
        if payload is None:
            return {
                "found": False,
                "task_id": task_id,
                "message": "Task notebook not found.",
            }
        return {
            "found": True,
            "task_id": task_id,
            "notebook": payload,
        }

    async def _session_revisit(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        session_id = self._coerce_session_id(tool_input, context)
        if not session_id:
            return {"error": True, "message": "session_id is required"}
        payload: dict[str, Any] = {
            "session_id": session_id,
            "turn_limit": min(max(1, self._coerce_int(tool_input.get("turn_limit"), 8)), 200),
            "raw_history_limit": min(max(1, self._coerce_int(tool_input.get("raw_history_limit"), 12)), 200),
        }
        task_id = str(tool_input.get("task_id") or "").strip() or (context.task_id if context else None)
        if task_id:
            payload["task_id"] = task_id
        request_id = str(tool_input.get("request_id") or "").strip() or (context.request_id if context else None)
        if request_id:
            payload["request_id"] = request_id
        return await self._request_gateway_json(
            "POST",
            "/internal/session/revisit",
            json_body=payload,
        )

    # ── Event Automations (Gateway Registry) ────────────────────

    async def _create_event_automation(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        raw_instruction = str(tool_input.get("raw_instruction") or "").strip()
        if not raw_instruction:
            return {"error": True, "message": "raw_instruction is required"}
        if not self.gateway_url:
            return {"error": True, "message": "Gateway event automation registry is not configured."}
        request_body: dict[str, Any] = {
            "event_type": str(tool_input.get("event_type") or "gmail.inbound").strip() or "gmail.inbound",
            "raw_instruction": raw_instruction,
            "condition": tool_input.get("condition") if isinstance(tool_input.get("condition"), dict) else {},
            "action": tool_input.get("action") if isinstance(tool_input.get("action"), dict) else {},
            "approval_policy": (
                tool_input.get("approval_policy")
                if isinstance(tool_input.get("approval_policy"), dict)
                else {}
            ),
            "status": "active",
            "source": "orchestrator",
        }
        automation_id = str(tool_input.get("automation_id") or "").strip()
        if automation_id:
            request_body["automation_id"] = automation_id
        label = str(tool_input.get("label") or "").strip()
        if label:
            request_body["label"] = label
        if context:
            if context.request_id:
                request_body["request_id"] = context.request_id
            if context.session_id:
                request_body["session_id"] = context.session_id
            if context.channel:
                request_body["channel"] = context.channel
        response = await self._request_gateway_json(
            "POST",
            "/internal/automations/events",
            json_body=request_body,
        )
        return {
            "created": True,
            "automation_id": response.get("automation_id"),
            "event_type": response.get("event_type"),
            "label": response.get("label"),
            "status": response.get("status"),
            "condition": response.get("condition"),
            "action": response.get("action"),
            "approval_policy": response.get("approval_policy"),
            "message": f"Event automation created: {response.get('label') or response.get('automation_id')}",
        }

    async def _list_event_automations(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        if not self.gateway_url:
            return {"error": True, "message": "Gateway event automation registry is not configured."}
        params: dict[str, Any] = {}
        event_type = str(tool_input.get("event_type") or "").strip()
        if event_type:
            params["event_type"] = event_type
        params["status_filter"] = str(tool_input.get("status") or "active").strip() or "active"
        params["limit"] = min(max(1, self._coerce_int(tool_input.get("limit"), 50)), 200)
        payload = await self._request_gateway_json(
            "GET",
            "/internal/automations/events",
            params=params,
        )
        return {"automations": payload.get("automations") or []}

    async def _delete_event_automation(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        automation_id = str(tool_input.get("automation_id") or "").strip()
        if not automation_id:
            return {"error": True, "message": "automation_id is required"}
        if not self.gateway_url:
            return {"error": True, "message": "Gateway event automation registry is not configured."}
        await self._request_gateway_json("DELETE", f"/internal/automations/events/{automation_id}")
        return {"deleted": True, "automation_id": automation_id, "message": "Event automation deactivated."}

    # ── Create Reminder (Gateway Scheduler) ─────────────────────

    async def _create_reminder(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        label = str(tool_input.get("label") or "").strip()
        cron_expression = str(tool_input.get("cron_expression") or "").strip()
        prompt = str(tool_input.get("prompt") or "").strip()
        one_shot = self._coerce_bool(tool_input.get("one_shot"), default=True)
        if not label or not cron_expression or not prompt:
            return {"error": True, "message": "label, cron_expression, and prompt are required"}
        if not self.gateway_url:
            return {"error": True, "message": "Gateway scheduler is not configured."}
        request_body: dict[str, Any] = {
            "label": label,
            "cron_expression": cron_expression,
            "prompt": prompt,
            "one_shot": one_shot,
            "source": "orchestrator",
        }
        timezone_name = str(tool_input.get("timezone") or "").strip()
        if timezone_name:
            request_body["timezone"] = timezone_name
        delivery_target = str(tool_input.get("delivery_target") or "").strip()
        if delivery_target:
            request_body["delivery_target"] = delivery_target
        delivery_channel = str(tool_input.get("delivery_channel") or "").strip()
        if delivery_channel:
            request_body["delivery_channel"] = delivery_channel
        context_summary = str(tool_input.get("context_summary") or "").strip()
        if context_summary:
            request_body["context_summary"] = context_summary
        if context:
            if context.request_id:
                request_body["request_id"] = context.request_id
            if context.session_id:
                request_body["session_id"] = context.session_id
            if context.channel:
                request_body["channel"] = context.channel
        response = await self._request_gateway_json(
            "POST",
            "/internal/scheduler/crons",
            json_body=request_body,
        )
        return {
            "created": True,
            "cron_id": response.get("cron_id"),
            "label": label,
            "cron_expression": cron_expression,
            "one_shot": one_shot,
            "timezone": response.get("timezone"),
            "delivery_target": response.get("delivery_target"),
            "next_fire_at": response.get("next_fire_at"),
            "next_fire_local": response.get("next_fire_local"),
            "delivery_channel": response.get("delivery_channel"),
            "resolved_delivery_channel": response.get("resolved_delivery_channel"),
            "context_summary": response.get("context_summary"),
            "message": f"Reminder created: {label}",
        }

    # ── List Reminders ──────────────────────────────────────────

    async def _list_reminders(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del tool_input, context
        if not self.gateway_url:
            return {"error": True, "message": "Gateway scheduler is not configured."}
        payload = await self._request_gateway_json("GET", "/internal/scheduler/crons")
        crons = payload.get("crons") or []
        manual_overrides = payload.get("manual_overrides") or []
        return {"reminders": crons, "manual_overrides": manual_overrides}

    # ── Delete Reminder ─────────────────────────────────────────

    async def _delete_reminder(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        del context
        cron_id = str(tool_input.get("cron_id") or "").strip()
        if not cron_id:
            return {"error": True, "message": "cron_id is required"}
        if not self.gateway_url:
            return {"error": True, "message": "Gateway scheduler is not configured."}
        await self._request_gateway_json("DELETE", f"/internal/scheduler/crons/{cron_id}")

        return {"deleted": True, "cron_id": cron_id, "message": "Reminder deleted."}

    # ── Internal helpers ────────────────────────────────────────

    async def _dispatch_specialist_agent(
        self,
        *,
        intent: str,
        payload: dict[str, Any],
        context: ToolExecutionContext | None,
        agent_id: str | None,
        wait_timeout_sec: float | None,
        input_artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if self._agent_dispatcher is None:
            return {"error": True, "message": f"{intent} is not configured in this orchestrator runtime."}
        if context is None or context.parent_task is None:
            return {"error": True, "message": f"{intent} requires the active parent task context."}

        result = await self._agent_dispatcher(
            parent_task=context.parent_task,
            intent=intent,
            input_payload=payload,
            input_artifacts=input_artifacts,
            agent_id=agent_id,
            wait_timeout_sec=wait_timeout_sec,
        )
        if isinstance(result, TaskInProgress):
            return {
                "error": True,
                "in_progress": True,
                "task_id": result.task_id,
                "idempotency_key": result.idempotency_key,
                "check_after_sec": result.check_after_sec,
                "message": f"{intent} is still running in the specialist agent.",
                "delegation": {
                    "intent": intent,
                    "agent_id": agent_id,
                    "task_id": result.task_id,
                },
            }

        if result.status != "completed":
            error = result.error
            return {
                "error": True,
                "code": error.code if error else "AGENT_FAILED",
                "retryable": error.retryable if error else False,
                "next_action": error.next_action if error else "escalate",
                "message": error.message if error else f"{intent} failed in the specialist agent.",
                "delegation": {
                    "intent": intent,
                    "agent_id": agent_id,
                    "task_id": (
                        result.output.get("delegated_task_id")
                        if isinstance(result.output, dict)
                        else None
                    ),
                },
            }

        output = result.output if isinstance(result.output, dict) else {}
        response = dict(output)
        # Reserved model-facing contract: specialists cannot self-assert that a
        # trusted client block will render.
        response.pop("_cosmic_ui", None)
        if result.artifacts and "artifacts" not in response:
            response["artifacts"] = [artifact.model_dump(mode="json") for artifact in result.artifacts]
        artifact_list = response.get("artifacts") if isinstance(response.get("artifacts"), list) else []
        if artifact_list:
            response.setdefault("artifacts_ready_in_response", True)
            response.setdefault("artifact_count", len(artifact_list))
        response.setdefault(
            "delegation",
            {
                "intent": intent,
                "agent_id": agent_id,
                "task_id": response.get("delegated_task_id") or response.get("task_id"),
            },
        )
        presentation_contract = self._trusted_inline_presentation_contract(
            intent=intent,
            response=response,
            context=context,
        )
        if presentation_contract:
            response["_cosmic_ui"] = presentation_contract
        return response

    @staticmethod
    def _trusted_inline_presentation_contract(
        *,
        intent: str,
        response: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> dict[str, Any] | None:
        """Describe trusted UI that will render beside the orchestrator response.

        This is a model-facing presentation contract, not a request to the model
        to construct UI. It is emitted only when the current client supports
        trusted response blocks and the specialist result contains every key the
        Gateway needs to build the corresponding block.
        """

        channel = str(context.channel if context else "").strip().lower()
        channel_platform = channel.split(":", 1)[0]
        if channel_platform not in {"desktop", "mobile"}:
            return None

        if intent == "gmail.draft_reply":
            account = response.get("account") if isinstance(response.get("account"), dict) else {}
            draft = response.get("draft") if isinstance(response.get("draft"), dict) else {}
            account_id = str(account.get("account_id") or "").strip()
            draft_id = str(response.get("draft_id") or "").strip()
            if (
                response.get("approval_required") is True
                and account_id
                and draft_id
                and draft
            ):
                return {
                    "version": 1,
                    "render": "trusted_inline_block",
                    "block_type": "gmail_draft_approval",
                    "covers": [
                        "draft status",
                        "sender account",
                        "recipients",
                        "subject",
                        "email body",
                        "approval actions",
                    ],
                    "response_mode": "brief_acknowledgement",
                    "instruction": (
                        "The client will render the complete Gmail draft and approval controls "
                        "beside your final response. Briefly confirm that the draft is ready. "
                        "Do not repeat the recipients, subject, body, or approval instructions "
                        "in Markdown. Mention only important context not represented by the card."
                    ),
                }

        if intent in {
            "calendar.create_event",
            "calendar.update_event",
            "calendar.respond_to_invite",
            "calendar.cancel_event",
        }:
            event = response.get("event") if isinstance(response.get("event"), dict) else {}
            if str(event.get("event_id") or event.get("summary") or "").strip():
                return {
                    "version": 1,
                    "render": "trusted_inline_block",
                    "block_type": "calendar_event",
                    "covers": [
                        "operation status",
                        "calendar account",
                        "event title",
                        "time",
                        "location",
                        "attendees",
                        "event links",
                    ],
                    "response_mode": "brief_acknowledgement",
                    "instruction": (
                        "The client will render the calendar event details beside your final "
                        "response, including its invitation response when relevant. Briefly confirm "
                        "the completed action. Do not repeat the event "
                        "details in Markdown unless a material warning or unresolved issue is "
                        "not represented by the card."
                    ),
                }

        return None

    async def _resolve_delegate_input_artifacts(
        self,
        tool_input: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        explicit = tool_input.get("input_artifacts")
        explicit_artifacts = [item for item in explicit if isinstance(item, dict)] if isinstance(explicit, list) else []
        artifact_ids = [
            str(item).strip()
            for item in (tool_input.get("artifact_ids") if isinstance(tool_input.get("artifact_ids"), list) else [])
            if str(item).strip()
        ]
        transient_explicit_artifact_ids = [
            str(item.get("artifact_id") or "").strip()
            for item in explicit_artifacts
            if self._artifact_descriptor_needs_gateway_resolution(item)
            and str(item.get("artifact_id") or "").strip()
        ]
        resolve_ids = self._dedupe_strings([*artifact_ids, *transient_explicit_artifact_ids])
        if not resolve_ids:
            return explicit_artifacts
        if not self.gateway_url:
            if artifact_ids:
                return {"error": True, "message": "Artifact resolution requires Gateway internal API configuration."}
            return explicit_artifacts
        session_id = self._coerce_session_id(tool_input, context)
        resolve_all_sessions = self._coerce_bool(tool_input.get("all_sessions"), default=False) or bool(
            transient_explicit_artifact_ids
        )
        try:
            payload = await self._request_gateway_json(
                "POST",
                "/internal/session/artifacts/resolve",
                json_body={
                    "session_id": session_id,
                    "artifact_ids": resolve_ids,
                    "all_sessions": resolve_all_sessions,
                },
            )
        except Exception as exc:
            if artifact_ids:
                return {"error": True, "message": f"Artifact resolution failed: {exc}"}
            logger.warning(
                "orchestrator.delegate_input_artifact_rehydrate_failed artifact_ids=%s error=%s",
                transient_explicit_artifact_ids,
                exc,
            )
            return explicit_artifacts
        if payload is None:
            return {"error": True, "message": "Artifact resolution did not return a payload."}
        resolved_artifacts = [item for item in payload.get("artifacts") or [] if isinstance(item, dict)]
        requested_ids = set(artifact_ids)
        resolved_requested_ids = {
            str(item.get("artifact_id") or "").strip()
            for item in resolved_artifacts
            if str(item.get("artifact_id") or "").strip() in requested_ids
        }
        if len(resolved_requested_ids) < len(requested_ids):
            return {
                "error": True,
                "message": str(payload.get("message") or "One or more requested artifacts could not be resolved."),
            }
        return self._merge_resolved_delegate_artifacts(
            explicit_artifacts=explicit_artifacts,
            resolved_artifacts=resolved_artifacts,
        )

    def _merge_resolved_delegate_artifacts(
        self,
        *,
        explicit_artifacts: list[dict[str, Any]],
        resolved_artifacts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        resolved_by_id = {
            str(item.get("artifact_id") or "").strip(): item
            for item in resolved_artifacts
            if str(item.get("artifact_id") or "").strip()
        }
        merged: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        replaced_ids: set[str] = set()

        def append_once(item: dict[str, Any]) -> None:
            artifact_id = str(item.get("artifact_id") or "").strip()
            path = str(item.get("path") or "").strip()
            dedupe_key = (artifact_id, path)
            if not any(dedupe_key) or dedupe_key in seen:
                return
            seen.add(dedupe_key)
            merged.append(item)

        for item in explicit_artifacts:
            artifact_id = str(item.get("artifact_id") or "").strip()
            replacement = resolved_by_id.get(artifact_id)
            if replacement is not None and self._artifact_descriptor_needs_gateway_resolution(item):
                append_once(replacement)
                replaced_ids.add(artifact_id)
                continue
            append_once(item)

        for item in resolved_artifacts:
            artifact_id = str(item.get("artifact_id") or "").strip()
            if artifact_id in replaced_ids:
                continue
            append_once(item)
        return merged

    def _artifact_descriptor_needs_gateway_resolution(self, artifact: dict[str, Any]) -> bool:
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        if not artifact_id:
            return False
        path = str(artifact.get("path") or "").strip()
        if not path:
            return True
        normalized = path.replace("\\", "/")
        return normalized.startswith("/files/input/") or "/files/input/" in normalized

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = str(value or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    async def _request_gateway_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        if not self.gateway_url:
            raise RuntimeError("Gateway internal API is not configured.")
        request_kwargs: dict[str, Any] = {
            "json": json_body,
            "params": params,
            "headers": self._gateway_headers(),
        }
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        response = await self._client.request(
            method,
            f"{self.gateway_url}{path}",
            **request_kwargs,
        )
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ToolHTTPError(
                status_code=response.status_code,
                message=f"Gateway API error (status={response.status_code}): {response.text[:300]}",
            )
        payload = self._response_json_as_object(response, service_name="gateway")
        return payload

    async def _request_cosmic_memory_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        if not self.cosmic_memory_url:
            raise RuntimeError("Memory service is not configured.")
        response = await self._client.request(
            method,
            f"{self.cosmic_memory_url}{path}",
            json=json_body,
            params=params,
            headers=self._memory_headers(),
        )
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ToolHTTPError(
                status_code=response.status_code,
                message=f"Memory API error (status={response.status_code}): {response.text[:300]}",
            )
        return self._response_json_as_object(response, service_name="cosmic-memory")

    def _response_json_as_object(self, response: httpx.Response, *, service_name: str) -> dict[str, Any]:
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"{service_name} returned a non-object payload")
        return payload

    def _coerce_session_id(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> str | None:
        session_id = str(tool_input.get("session_id") or "").strip()
        if session_id:
            return session_id
        if context and context.session_id:
            return context.session_id
        return None

    def _coerce_task_id(
        self,
        tool_input: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> str | None:
        task_id = str(tool_input.get("task_id") or "").strip()
        if task_id:
            return task_id
        if context and context.task_id:
            return context.task_id
        return None

    def _normalize_memory_search_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        items = result.get("items")
        if not isinstance(items, list):
            items = result.get("results")
        normalized_items = [item for item in items or [] if isinstance(item, dict)]
        result["items"] = normalized_items
        result.setdefault("results", normalized_items)
        for key in ("entities", "relations", "episodes", "search_plan"):
            value = result.get(key)
            result[key] = value if isinstance(value, list) else []
        if not normalized_items and not any(result[key] for key in ("entities", "relations", "episodes")):
            result.setdefault("message", "No matching memories found.")
        return result

    def _normalize_memory_record_result(
        self,
        payload: dict[str, Any],
        *,
        requested_memory_id: str,
    ) -> dict[str, Any]:
        record = dict(payload)
        memory_id = str(record.get("memory_id") or requested_memory_id).strip() or requested_memory_id
        tags = record.get("tags")
        metadata = record.get("metadata")
        provenance = record.get("provenance")
        return {
            "found": True,
            "memory_id": memory_id,
            "kind": record.get("kind"),
            "title": record.get("title"),
            "content": record.get("content"),
            "tags": tags if isinstance(tags, list) else [],
            "metadata": metadata if isinstance(metadata, dict) else {},
            "provenance": provenance if isinstance(provenance, dict) else {},
            "status": record.get("status"),
            "version": record.get("version"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "supersedes": record.get("supersedes"),
            "superseded_by": record.get("superseded_by"),
            "record": record,
        }

    def _build_memory_metadata(self, context: ToolExecutionContext | None) -> dict[str, Any]:
        if context is None:
            return {}
        metadata = {
            "stored_by": "cosmic/orchestrator:1.0.0",
            "task_id": context.task_id,
            "request_id": context.request_id,
            "session_id": context.session_id,
            "channel": context.channel,
            "source": context.source,
            "source_id": context.source_id,
        }
        return self._clean_mapping(metadata)

    def _build_orchestrator_provenance(self, context: ToolExecutionContext | None) -> dict[str, Any]:
        provenance = {
            "source_kind": "orchestrator_tool",
            "created_by": "cosmic/orchestrator:1.0.0",
            "task_id": context.task_id if context else None,
            "request_id": context.request_id if context else None,
            "session_id": context.session_id if context else None,
            "channel": context.channel if context else None,
            "source": context.source if context else None,
            "source_id": context.source_id if context else None,
        }
        return self._clean_mapping(provenance)

    def _derive_memory_title(self, content: str) -> str:
        normalized = str(content or "").strip()
        if not normalized:
            return "Untitled memory"
        title = normalized.splitlines()[0].strip()
        if len(title) > 72:
            title = title[:69].rstrip() + "..."
        return title or "Untitled memory"

    def _read_heartbeat_notes_text(self) -> str:
        path = self.heartbeat_notes_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(HEARTBEAT_NOTES_HEADER, encoding="utf-8")
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"Unable to read heartbeat notes: {exc}") from exc
        if not text.strip():
            return HEARTBEAT_NOTES_HEADER
        return text

    def _normalize_heartbeat_notes_fragment(self, value: Any) -> str:
        """Bound a single note the caller is adding.

        Head-truncation is correct here: a fragment is one note, and a note
        that alone exceeds the whole-document budget is pathological.
        """
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) > HEARTBEAT_NOTES_MAX_CHARS:
            text = text[:HEARTBEAT_NOTES_MAX_CHARS].rstrip()
        return text

    def _normalize_heartbeat_notes_document(self, value: Any) -> str:
        """Bound the whole scratchpad, keeping standing state and new entries.

        This used to head-truncate as well, which is a latent trap on an
        append-only file: once the document reached the cap, every append would
        be chopped off the end and silently discarded, leaving the scratchpad
        append-proof with no error anywhere. The file was growing ~4KB/day
        against a 32k cap when this was found.
        """
        text = str(value or "").strip()
        if not text:
            return ""
        return truncate_keeping_newest(text, limit=HEARTBEAT_NOTES_MAX_CHARS)

    def _write_heartbeat_notes_text(self, value: str) -> str:
        text = self._normalize_heartbeat_notes_document(value) or HEARTBEAT_NOTES_HEADER
        if not text.lstrip().startswith("# COSMIC Heartbeat Notes"):
            text = f"{HEARTBEAT_NOTES_HEADER}{text}"
        if not text.endswith("\n"):
            text += "\n"
        try:
            self.heartbeat_notes_path.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_notes_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Unable to write heartbeat notes: {exc}") from exc
        return text

    def _normalize_memory_write_kind(self, kind: str) -> str | None:
        normalized = str(kind or "").strip().lower()
        if not normalized:
            return "agent_note"
        alias_map = {
            "agent_note": "agent_note",
            "note": "agent_note",
            "user_data": "user_data",
            "preference": "user_data",
            "fact": "user_data",
            "relationship": "user_data",
            "goal": "user_data",
            "event": "user_data",
        }
        return alias_map.get(normalized)

    def _normalize_string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item.strip() for item in (str(entry or "") for entry in value) if item.strip()]

    def _coerce_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _coerce_bool(self, value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return bool(value)

    def _merge_dicts(self, base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in extra.items():
            if value is None:
                continue
            merged[str(key)] = value
        return self._clean_mapping(merged)

    def _clean_mapping(self, value: dict[str, Any]) -> dict[str, Any]:
        return {
            str(key): item
            for key, item in value.items()
            if item not in ("", None, [], {})
        }

    def _memory_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.gateway_internal_token:
            headers["X-Internal-Token"] = self.gateway_internal_token
        return headers

    def _gateway_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.gateway_internal_token:
            headers["X-Internal-Token"] = self.gateway_internal_token
        return headers
