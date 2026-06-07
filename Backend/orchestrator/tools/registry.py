from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable


ProgressBuilder = Callable[[dict[str, Any]], str]

_DOCS_AGENT_ID = "cosmic/docs-parser-agent:1.0.0"
_TABULAR_AGENT_ID = "cosmic/tabular-agent:1.0.0"
_FIRECRAWL_AGENT_ID = "cosmic/firecrawl-web-scrape-agent:1.0.0"
_X_SEARCH_AGENT_ID = "cosmic/x-twitter-search-agent:1.0.0"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    api_definition: dict[str, Any]
    group: str
    prompt_summary: str
    progress_builder: ProgressBuilder | None = None
    handler_method: str | None = None
    read_only: bool = False
    exposed_to_model: bool = True
    specialist_agent_id: str | None = None

    @property
    def is_local(self) -> bool:
        return self.handler_method is not None

    def to_definition(self) -> dict[str, Any]:
        return deepcopy(self.api_definition)

    def is_visible_to_model(self, featured_agent_ids: set[str] | None = None) -> bool:
        if self.specialist_agent_id:
            return bool(featured_agent_ids and self.specialist_agent_id in featured_agent_ids)
        return self.exposed_to_model


def _preview_list(value: Any, *, limit: int = 2) -> str:
    if not isinstance(value, list):
        return ""
    normalized = [str(item or "").strip() for item in value]
    items = [item for item in normalized if item][:limit]
    if not items:
        return ""
    preview = ", ".join(items)
    if len(normalized) > len(items):
        preview += ", ..."
    return preview


def _web_search_progress(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip()
    return f"Searching the web for: {query}" if query else "Searching the web..."


def _web_fetch_progress(tool_input: dict[str, Any]) -> str:
    url = str(tool_input.get("url") or "").strip()
    return f"Fetching: {url}" if url else "Fetching web page..."


def _perplexity_progress(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip()
    return f"Researching: {query}" if query else "Conducting research..."


def _agent_catalog_search_progress(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip()
    return f"Looking for a specialist for: {query}" if query else "Looking for a specialist agent..."


def _delegate_to_agent_progress(tool_input: dict[str, Any]) -> str:
    intent = str(tool_input.get("intent") or "").strip()
    payload = tool_input.get("input") if isinstance(tool_input.get("input"), dict) else {}
    intent_label = intent or "specialist task"
    url = str(payload.get("url") or "").strip()
    if url:
        return f"Delegating {intent_label} for {url}..."
    urls = payload.get("urls")
    if isinstance(urls, list):
        cleaned = [str(item or "").strip() for item in urls if str(item or "").strip()]
        if len(cleaned) == 1:
            return f"Delegating {intent_label} for {cleaned[0]}..."
        if cleaned:
            return f"Delegating {intent_label} for {len(cleaned)} pages..."
    query = str(payload.get("query") or "").strip()
    if query:
        return f"Delegating {intent_label} for: {query}"
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        return f"Delegating {intent_label} for {session_id}..."
    return f"Delegating to specialist intent: {intent_label}" if intent else "Delegating to a specialist agent..."


def _cosmic_code_execution_progress(tool_input: dict[str, Any]) -> str:
    description = str(tool_input.get("description") or "").strip()
    if description:
        return f"Running local code sandbox: {description[:96]}{'...' if len(description) > 96 else ''}"
    return "Running local code sandbox..."


def _artifact_lookup_progress(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip()
    return f"Looking for a previous file: {query}" if query else "Looking for a previous produced file..."


def _artifact_redeliver_progress(tool_input: dict[str, Any]) -> str:
    artifact_id = str(tool_input.get("artifact_id") or "").strip()
    return f"Re-surfacing file {artifact_id}..." if artifact_id else "Re-surfacing a previous produced file..."


def _wishlist_search_progress(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip()
    return f"Checking COSMIC's capability wishlist for: {query}" if query else "Checking COSMIC's capability wishlist..."


def _wishlist_capture_progress(tool_input: dict[str, Any]) -> str:
    title = str(tool_input.get("title") or "").strip()
    return f"Recording capability gap: {title}" if title else "Recording a capability gap for COSMIC..."


def _tool_opportunity_capture_progress(tool_input: dict[str, Any]) -> str:
    title = str(tool_input.get("title") or "").strip()
    return f"Saving custom tool opportunity: {title}" if title else "Saving a custom tool opportunity..."


def _tool_opportunities_list_progress(tool_input: dict[str, Any]) -> str:
    del tool_input
    return "Reviewing existing custom tool opportunities..."


def _tool_opportunity_update_progress(tool_input: dict[str, Any]) -> str:
    opportunity_id = str(tool_input.get("opportunity_id") or "").strip()
    return f"Updating custom tool opportunity {opportunity_id}" if opportunity_id else "Updating a custom tool opportunity..."


def _docs_browse_progress(tool_input: dict[str, Any]) -> str:
    bundle_id = str(tool_input.get("bundle_id") or "").strip()
    index_kind = str(tool_input.get("index_kind") or "documents").strip() or "documents"
    if bundle_id:
        return f"Browsing {index_kind} in parsed bundle {bundle_id}..."
    return f"Browsing parsed document {index_kind}..."


def _docs_search_progress(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip()
    return f"Searching parsed documents for: {query}" if query else "Searching parsed documents..."


def _docs_read_progress(tool_input: dict[str, Any]) -> str:
    read_kind = str(tool_input.get("read_kind") or "").strip()
    if read_kind == "document":
        return "Reading canonical full-document markdown..."
    if read_kind in {"page_range", "slide_range"}:
        return f"Reading parsed {read_kind.replace('_', ' ')}..."
    if read_kind == "markdown_window":
        anchor_id = str(tool_input.get("anchor_id") or "").strip()
        return f"Reading parsed markdown window around {anchor_id}..." if anchor_id else "Reading parsed markdown window..."
    section_id = str(tool_input.get("section_id") or "").strip()
    if section_id:
        return f"Reading parsed section {section_id}..."
    chunk_ids = tool_input.get("chunk_ids")
    if isinstance(chunk_ids, list) and chunk_ids:
        return f"Reading {len(chunk_ids)} parsed chunk(s)..."
    bundle_id = str(tool_input.get("bundle_id") or "").strip()
    return f"Reading parsed bundle {bundle_id}..." if bundle_id else "Reading parsed document content..."


def _docs_fetch_asset_progress(tool_input: dict[str, Any]) -> str:
    asset_id = str(tool_input.get("asset_id") or "").strip()
    return f"Fetching parsed asset {asset_id}..." if asset_id else "Fetching a parsed asset..."


def _sheets_browse_progress(tool_input: dict[str, Any]) -> str:
    bundle_id = str(tool_input.get("bundle_id") or "").strip()
    return f"Browsing spreadsheet bundle {bundle_id}..." if bundle_id else "Browsing spreadsheet bundle..."


def _sheets_schema_progress(tool_input: dict[str, Any]) -> str:
    sheet_id = str(tool_input.get("sheet_id") or "").strip()
    return f"Loading sheet schema{f' for {sheet_id}' if sheet_id else ''}..."


def _sheets_preview_progress(tool_input: dict[str, Any]) -> str:
    sheet_id = str(tool_input.get("sheet_id") or "").strip()
    return f"Previewing sheet {sheet_id}..." if sheet_id else "Previewing spreadsheet sheet..."


def _sheets_query_progress(tool_input: dict[str, Any]) -> str:
    return "Running deterministic SQL over parsed spreadsheet data..."


def _sheets_export_progress(tool_input: dict[str, Any]) -> str:
    return "Exporting spreadsheet query results to artifacts..."


def _sheets_export_sheet_progress(tool_input: dict[str, Any]) -> str:
    sheet_id = str(tool_input.get("sheet_id") or "").strip()
    fmt = str(tool_input.get("format") or "csv").strip().lower()
    if sheet_id:
        return f"Exporting sheet {sheet_id} as {fmt}..."
    return f"Exporting spreadsheet sheet as {fmt}..."


def _sheets_reason_progress(tool_input: dict[str, Any]) -> str:
    g = str(tool_input.get("goal") or "").strip()
    if g:
        return f"Tabular specialist reasoning ({g[:72]}{'…' if len(g) > 72 else ''})"
    return "Tabular specialist internal reasoning over spreadsheet bundle."


def _sheets_create_sheet_progress(tool_input: dict[str, Any]) -> str:
    sheet_id = str(tool_input.get("sheet_id") or "").strip()
    return f"Creating sheet {sheet_id}..." if sheet_id else "Creating spreadsheet sheet..."


def _sheets_create_workbook_progress(tool_input: dict[str, Any]) -> str:
    filename = str(tool_input.get("filename") or "").strip()
    return f"Creating workbook {filename}..." if filename else "Creating spreadsheet workbook..."


def _docs_reinspect_asset_progress(tool_input: dict[str, Any]) -> str:
    asset_id = str(tool_input.get("asset_id") or "").strip()
    question = str(tool_input.get("question") or "").strip()
    if asset_id and question:
        return f"Reinspecting asset {asset_id}: {question}"
    return f"Reinspecting asset {asset_id}..." if asset_id else "Reinspecting a parsed visual asset..."


def _firecrawl_scrape_progress(tool_input: dict[str, Any]) -> str:
    url = str(tool_input.get("url") or "").strip()
    return f"Scraping via Firecrawl: {url}" if url else "Scraping a page via Firecrawl..."


def _firecrawl_extract_progress(tool_input: dict[str, Any]) -> str:
    urls = tool_input.get("urls")
    if isinstance(urls, list):
        cleaned = [str(item or "").strip() for item in urls if str(item or "").strip()]
        if len(cleaned) == 1:
            return f"Extracting structured data via Firecrawl: {cleaned[0]}"
        if cleaned:
            return f"Extracting structured data via Firecrawl from {len(cleaned)} pages..."
    return "Extracting structured data via Firecrawl..."


def _firecrawl_agent_progress(tool_input: dict[str, Any]) -> str:
    prompt = str(tool_input.get("prompt") or "").strip()
    if prompt:
        short = prompt[:80] + ("..." if len(prompt) > 80 else "")
        return f"Firecrawl autonomous agent: {short}"
    return "Running Firecrawl autonomous agent..."


def _firecrawl_recall_progress(tool_input: dict[str, Any]) -> str:
    session_id = str(tool_input.get("session_id") or "").strip()
    return f"Reviewing prior Firecrawl runs for {session_id}..." if session_id else "Reviewing prior Firecrawl runs..."


def _x_search_progress(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip()
    return f"Searching X for: {query}" if query else "Searching X..."


def _x_recall_progress(tool_input: dict[str, Any]) -> str:
    session_id = str(tool_input.get("session_id") or "").strip()
    return f"Reviewing prior X search runs for {session_id}..." if session_id else "Reviewing prior X search runs..."


def _memory_search_progress(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip()
    seed_ids = _preview_list(tool_input.get("seed_memory_ids"))
    if seed_ids:
        return (
            f"Exploring memories related to {seed_ids} for: {query}"
            if query else
            f"Exploring memories related to {seed_ids}..."
        )
    seed_entities = _preview_list(tool_input.get("seed_entities"))
    if seed_entities:
        return (
            f"Tracing memory around {seed_entities} for: {query}"
            if query else
            f"Tracing memory around {seed_entities}..."
        )
    return f"Searching memory for: {query}" if query else "Searching memory..."


def _memory_fetch_progress(tool_input: dict[str, Any]) -> str:
    memory_id = str(tool_input.get("memory_id") or "").strip()
    return f"Loading full memory block {memory_id}..." if memory_id else "Loading full memory block..."


def _memory_write_progress(tool_input: dict[str, Any]) -> str:
    title = str(tool_input.get("title") or "").strip()
    if title:
        return f"Saving to memory: {title}"
    kind = str(tool_input.get("kind") or "").strip()
    return f"Saving {kind} to memory..." if kind else "Saving to memory..."


def _heartbeat_notes_progress(tool_input: dict[str, Any]) -> str:
    action = str(tool_input.get("action") or "read").strip().lower()
    if action in {"append", "replace", "remove", "clear"}:
        return f"Updating heartbeat notes ({action})..."
    return "Reading heartbeat notes..."


def _memory_core_fact_progress(tool_input: dict[str, Any]) -> str:
    title = str(tool_input.get("title") or "").strip()
    if title:
        return f"Saving core fact: {title}"
    canonical_key = str(tool_input.get("canonical_key") or "").strip()
    if canonical_key:
        return f"Saving core fact {canonical_key}..."
    return "Saving core fact..."


def _session_state_progress(tool_input: dict[str, Any]) -> str:
    session_id = str(tool_input.get("session_id") or "").strip()
    return f"Loading session state for {session_id}..." if session_id else "Loading session state..."


def _session_turns_progress(tool_input: dict[str, Any]) -> str:
    session_id = str(tool_input.get("session_id") or "").strip()
    return f"Reviewing turn ledger for {session_id}..." if session_id else "Reviewing session turn ledger..."


def _session_history_progress(tool_input: dict[str, Any]) -> str:
    session_id = str(tool_input.get("session_id") or "").strip()
    return f"Loading detailed history for {session_id}..." if session_id else "Loading detailed session history..."


def _task_notebook_progress(tool_input: dict[str, Any]) -> str:
    task_id = str(tool_input.get("task_id") or "").strip()
    return f"Loading notebook for {task_id}..." if task_id else "Loading the current task notebook..."


def _session_revisit_progress(tool_input: dict[str, Any]) -> str:
    session_id = str(tool_input.get("session_id") or "").strip()
    return f"Revisiting exact history for {session_id}..." if session_id else "Revisiting exact session history..."


def _create_reminder_progress(tool_input: dict[str, Any]) -> str:
    label = str(tool_input.get("label") or "").strip()
    delivery_target = str(tool_input.get("delivery_target") or "").strip()
    if label and delivery_target:
        return f"Creating reminder for {delivery_target}: {label}"
    return f"Creating reminder: {label}" if label else "Creating reminder..."


def _create_event_automation_progress(tool_input: dict[str, Any]) -> str:
    label = str(tool_input.get("label") or "").strip()
    event_type = str(tool_input.get("event_type") or "event").strip()
    return f"Creating event automation for {event_type}: {label}" if label else f"Creating event automation for {event_type}..."


def _delete_event_automation_progress(tool_input: dict[str, Any]) -> str:
    automation_id = str(tool_input.get("automation_id") or "").strip()
    return f"Removing event automation {automation_id}..." if automation_id else "Removing event automation..."


def _delete_reminder_progress(tool_input: dict[str, Any]) -> str:
    cron_id = str(tool_input.get("cron_id") or "").strip()
    return f"Removing reminder {cron_id}..." if cron_id else "Removing reminder..."


_MODEL_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="web_search",
        api_definition={"type": "web_search_20260209", "name": "web_search"},
        group="web",
        prompt_summary="Fast current-information lookup on the live web. Use this first for news, docs, prices, weather, laws, or anything time-sensitive.",
        progress_builder=_web_search_progress,
        read_only=True,
    ),
    ToolSpec(
        name="web_fetch",
        api_definition={"type": "web_fetch_20260209", "name": "web_fetch"},
        group="web",
        prompt_summary="Fetch and read the full text of a specific URL after you know which page you need.",
        progress_builder=_web_fetch_progress,
        read_only=True,
    ),
    ToolSpec(
        name="perplexity_research",
        api_definition={
            "name": "perplexity_research",
            "description": (
                "Conduct deeper multi-source research using Perplexity. Use this when a quick web search is not enough "
                "and you need synthesis, comparison, or a more thorough answer. Do not use this for X/Twitter platform "
                "search when the X specialist is available."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The research query. Be specific and descriptive for best results.",
                    },
                },
                "required": ["query"],
            },
        },
        group="research",
        prompt_summary=(
            "Deep synthesized research across multiple sources when a quick web lookup is not enough. "
            "Not the preferred tool for X/Twitter platform search."
        ),
        progress_builder=_perplexity_progress,
        handler_method="_perplexity_research",
        read_only=True,
    ),
    ToolSpec(
        name="agent_catalog_search",
        api_definition={
            "name": "agent_catalog_search",
            "description": (
                "Search the live specialist-agent catalog for registered capabilities, health, and compact input-schema hints. "
                "Use this when local tools are insufficient or when you need to discover the right specialist intent before delegation."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Capability search query such as 'rendered page scrape', 'structured extraction', or 'browser automation'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matching specialist intents to return. Default 5.",
                        "default": 5,
                    },
                    "require_healthy": {
                        "type": "boolean",
                        "description": "When true, only return specialists that currently have a healthy live instance. Default true.",
                        "default": True,
                    },
                },
            },
        },
        group="specialists",
        prompt_summary="Discover registered specialist agents on demand, including exact intents and compact input-schema hints, before delegating.",
        progress_builder=_agent_catalog_search_progress,
        handler_method="_agent_catalog_search",
        read_only=True,
    ),
    ToolSpec(
        name="delegate_to_agent",
        api_definition={
            "name": "delegate_to_agent",
            "description": (
                "Dispatch a child task to a registered specialist agent by exact intent. "
                "Use this only after you know the right intent, usually by first calling agent_catalog_search."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "Exact specialist intent name, such as firecrawl.scrape or browser.extract.",
                    },
                    "input": {
                        "type": "object",
                        "description": "Structured payload for the specialist intent. Keep it minimal and match the schema hints returned by agent_catalog_search. For alpha.execute, write a concise high-level goal and pass bulky files or parsed documents by artifact_ids/input_artifacts; if a document has a bundle_id, pass the source artifact reference so parsed bundle files can be staged. Let Alpha use its configured/auto harness unless the user explicitly requires one provider, and do not request cross-provider fallback unless the user has clearly allowed it.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional exact agent_id if you want a specific registered specialist version.",
                    },
                    "wait_timeout_sec": {
                        "type": "number",
                        "description": "Optional override for how long to wait before returning an in-progress result.",
                    },
                    "artifact_ids": {
                        "type": "array",
                        "description": (
                            "Optional previously produced artifact IDs to resolve and pass to the specialist via "
                            "TaskEnvelope.input_artifacts."
                        ),
                        "items": {"type": "string"},
                    },
                    "input_artifacts": {
                        "type": "array",
                        "description": (
                            "Optional explicit artifact descriptors to pass to the specialist via "
                            "TaskEnvelope.input_artifacts. Prefer artifact_ids when reusing prior COSMIC-produced files."
                        ),
                        "items": {"type": "object"},
                    },
                    "all_sessions": {
                        "type": "boolean",
                        "description": "When true, resolve artifact_ids across all known sessions instead of only the current session.",
                        "default": False,
                    },
                },
                "required": ["intent", "input"],
            },
        },
        group="specialists",
        prompt_summary="Delegate specialist work by exact intent after discovery. For Alpha project work, pass artifact_ids/input_artifacts for large files or parsed documents instead of pasting their full contents into the input; bundle ids alone are metadata, while artifact references let Alpha receive concrete workspace files.",
        progress_builder=_delegate_to_agent_progress,
        handler_method="_delegate_to_agent",
    ),
    ToolSpec(
        name="cosmic_code_execution",
        api_definition={
            "name": "cosmic_code_execution",
            "description": (
                "Run a bounded local Python sandbox for calculations, quick validation, data transforms, "
                "small chart/file generation, and artifact-producing snippets. This is not a shell and is not "
                "for project edits, deployment, screenshots, network access, or long-running work; use Alpha for those. "
                "Do not use this for maps, directions, route alternatives, place lookup visuals, or geocoding workflows; "
                "use the map.render specialist instead because local HTML/Folium outputs are delivered as downloads, "
                "while map.render produces inline COSMIC map artifacts. "
                "Write files that should be delivered to the user under the relative `outputs/` directory."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python code to execute. Use print() for concise results. Write deliverable files under "
                            "`outputs/`, for example outputs/result.csv or outputs/chart.png."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Short human-readable reason for this sandbox run.",
                    },
                    "packages": {
                        "type": "array",
                        "description": (
                            "Optional pip packages to install into this sandbox run's isolated cached venv. "
                            "Use this for normal scientific/charting packages such as matplotlib, pandas, numpy, or openpyxl. "
                            "Use Alpha for package-heavy projects or non-Python setup."
                        ),
                        "items": {"type": "string"},
                    },
                    "timeout_sec": {
                        "type": "number",
                        "description": "Optional timeout, capped by the orchestrator sandbox setting.",
                    },
                },
                "required": ["code"],
            },
        },
        group="code",
        prompt_summary=(
            "Bounded local Python sandbox for calculations, quick checks, small data transforms, and generated "
            "files. Write deliverables to `outputs/`. Use map.render, not this sandbox, for maps/routes/place visuals "
            "that should appear inline. Use Alpha instead for shell/project/deployment/long-running work."
        ),
        progress_builder=_cosmic_code_execution_progress,
        handler_method="_cosmic_code_execution",
    ),
    ToolSpec(
        name="artifact_lookup",
        api_definition={
            "name": "artifact_lookup",
            "description": (
                "Search previously produced COSMIC files by name or description so you can reuse or re-deliver them."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Filename or natural-language description of the file to find.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session to search. Defaults to the current session when available.",
                    },
                    "all_sessions": {
                        "type": "boolean",
                        "description": "When true, search across all known sessions instead of only the current session.",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return. Default 5.",
                        "default": 5,
                    },
                },
            },
        },
        group="artifacts",
        prompt_summary="Find prior produced files by filename or description before re-delivering them or passing them back into a specialist.",
        progress_builder=_artifact_lookup_progress,
        handler_method="_artifact_lookup",
        read_only=True,
    ),
    ToolSpec(
        name="artifact_redeliver",
        api_definition={
            "name": "artifact_redeliver",
            "description": (
                "Re-surface a previously produced COSMIC file in the current response so the user can download it again."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": "Exact artifact_id of the previously produced file to re-surface.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session to resolve from. Defaults to the current session when available.",
                    },
                    "all_sessions": {
                        "type": "boolean",
                        "description": "When true, resolve across all known sessions instead of only the current session.",
                        "default": False,
                    },
                },
                "required": ["artifact_id"],
            },
        },
        group="artifacts",
        prompt_summary="Re-surface a previous deliverable file as a Produced Files card in the current response.",
        progress_builder=_artifact_redeliver_progress,
        handler_method="_artifact_redeliver",
        read_only=True,
    ),
    ToolSpec(
        name="cosmics_capability_wishlist_search",
        api_definition={
            "name": "cosmics_capability_wishlist_search",
            "description": (
                "Search COSMIC's capability wishlist for existing or similar missing capabilities. "
                "Use this when you need to inspect previously recorded gaps, roadmap items, or similar wishes. "
                "This is for retrieval and inspection; capture does not require a pre-search just for dedupe."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Capability-gap search query, feature area, or problem description.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matches to return. Default 3.",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
        group="planning",
        prompt_summary="Inspect the canonical COSMIC capability wishlist for similar missing capabilities or roadmap items when you need retrieval or awareness.",
        progress_builder=_wishlist_search_progress,
        handler_method="_cosmics_capability_wishlist_search",
        read_only=True,
    ),
    ToolSpec(
        name="cosmics_capability_wishlist_capture",
        api_definition={
            "name": "cosmics_capability_wishlist_capture",
            "description": (
                "Capture a meaningful missing COSMIC capability into the canonical capability wishlist. "
                "You can call this directly when you find a real gap; the backend automatically searches for similar entries, deduplicates, and may update an existing item."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short operator-readable capability title.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Why this missing capability matters and what gap it would solve.",
                    },
                    "desired_outcome": {
                        "type": "string",
                        "description": "Optional concrete desired outcome once this capability exists.",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Optional product area such as scheduling, desktop_ui, memory, agents, or communications.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional short tags for retrieval and grouping.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Optional concise evidence from the current interaction showing why this capability is needed.",
                    },
                },
                "required": ["title", "summary"],
            },
        },
        group="planning",
        prompt_summary="Capture a real missing capability directly when you notice COSMIC would materially help the user better if it already had that capability.",
        progress_builder=_wishlist_capture_progress,
        handler_method="_cosmics_capability_wishlist_capture",
    ),
    ToolSpec(
        name="custom_tool_opportunities_list",
        api_definition={
            "name": "custom_tool_opportunities_list",
            "description": (
                "List existing My Tools opportunities and their lifecycle state. Use this before saving a suggestion when continuity or duplication matters, "
                "and when deciding whether to continue an existing Alpha-linked tool instead of proposing a new one."
            ),
            "input_schema": {"type": "object", "properties": {}},
        },
        group="planning",
        prompt_summary="Review My Tools suggestions, accepted builds, and live Alpha-linked tools.",
        progress_builder=_tool_opportunities_list_progress,
        handler_method="_custom_tool_opportunities_list",
        read_only=True,
    ),
    ToolSpec(
        name="custom_tool_opportunity_capture",
        api_definition={
            "name": "custom_tool_opportunity_capture",
            "description": (
                "Save a high-value opportunity for a persistent custom site, dashboard, tracker, portal, workspace, or utility. "
                "Use this when a purpose-built interface would materially improve an ongoing user goal. This records the suggestion; it does not build or deploy it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "tool_type": {"type": "string", "enum": ["site", "dashboard", "tracker", "portal", "workspace", "utility"]},
                    "goal": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "proposed_features": {"type": "array", "items": {"type": "string"}},
                    "helpful_materials": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional inputs that could improve the result but must not be treated as blockers.",
                    },
                    "required_inputs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Only inputs that are truly impossible to proceed without.",
                    },
                    "data_sources": {"type": "array", "items": {"type": "string"}},
                    "expected_value": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["title", "tool_type", "goal", "reasoning"],
            },
        },
        group="planning",
        prompt_summary="Record a valuable persistent custom-tool suggestion in My Tools without building it yet.",
        progress_builder=_tool_opportunity_capture_progress,
        handler_method="_custom_tool_opportunity_capture",
    ),
    ToolSpec(
        name="custom_tool_opportunity_update",
        api_definition={
            "name": "custom_tool_opportunity_update",
            "description": (
                "Update an existing My Tools opportunity after the user accepts, declines, defers, after Alpha begins/completes the build, "
                "or while intelligently reviewing unaccepted suggestions. Use it to refine useful ideas and link the real Alpha project, repository, "
                "and deployment rather than creating a second project registry. During the automatic weekly My Tools review, unaccepted ideas may be "
                "refined, deferred, or archived, but accepted/building/live tools must not be materially rewritten; create a separate improvement "
                "opportunity instead."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "opportunity_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["candidate", "suggested", "accepted", "building", "live", "declined", "deferred", "archived", "failed"],
                    },
                    "alpha_project_id": {"type": "string"},
                    "build_task_id": {"type": "string"},
                    "deployment_url": {"type": "string"},
                    "repo_url": {"type": "string"},
                    "user_feedback": {"type": "string"},
                    "declined_reason": {"type": "string"},
                    "health_status": {"type": "string"},
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "expected_value": {"type": "string"},
                    "proposed_features": {"type": "array", "items": {"type": "string"}},
                    "helpful_materials": {"type": "array", "items": {"type": "string"}},
                    "required_inputs": {"type": "array", "items": {"type": "string"}},
                    "data_sources": {"type": "array", "items": {"type": "string"}},
                    "defer_until": {"type": "string"},
                    "review_reason": {
                        "type": "string",
                        "description": "Concise rationale for an autonomous review edit or lifecycle decision.",
                    },
                },
                "required": ["opportunity_id"],
            },
        },
        group="planning",
        prompt_summary="Update a My Tools opportunity and link it to Alpha execution/deployment state.",
        progress_builder=_tool_opportunity_update_progress,
        handler_method="_custom_tool_opportunity_update",
    ),
    ToolSpec(
        name="docs_browse",
        api_definition={
            "name": "docs_browse",
            "description": (
                "Browse a parsed document bundle without loading full content. "
                "Use this to inspect document, section, page, slide, chunk, table, figure, asset, and visual-enrichment indexes after uploaded documents have been parsed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {
                        "type": "string",
                        "description": "Parsed bundle ID shown in uploaded document metadata, such as bundle_docs_001.",
                    },
                    "index_kind": {
                        "type": "string",
                        "description": "Which index to browse: documents, sections, pages, slides, chunks, tables, figures, or assets.",
                        "enum": ["documents", "sections", "pages", "slides", "chunks", "tables", "figures", "assets"],
                        "default": "documents",
                    },
                    "doc_id": {
                        "type": "string",
                        "description": "Optional doc_id when you want a specific document inside a multi-document bundle.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Optional limit for chunk index browsing. Default 20.",
                        "default": 20,
                    }
                },
                "required": ["bundle_id"],
            },
        },
        group="documents",
        prompt_summary="Browse parsed uploaded documents by bundle, document, section, page, slide, chunk, figure, table, or asset index before doing selective reads.",
        progress_builder=_docs_browse_progress,
        handler_method="_docs_browse",
        read_only=True,
        specialist_agent_id=_DOCS_AGENT_ID,
    ),
    ToolSpec(
        name="docs_search",
        api_definition={
            "name": "docs_search",
            "description": (
                "Search a parsed document bundle for relevant sections or chunks. "
                "Use this after uploaded documents have been parsed instead of pretending you directly read the whole file. "
                "Results may include recommended section or chunk follow-up reads."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {
                        "type": "string",
                        "description": "Parsed bundle ID shown in uploaded document metadata, such as bundle_docs_001.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query for the parsed documents.",
                    },
                    "search_kind": {
                        "type": "string",
                        "description": "Search sections for broader semantic coverage or chunks for tighter excerpts.",
                        "enum": ["sections", "chunks"],
                        "default": "chunks",
                    },
                    "doc_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional doc_ids to limit the search to specific documents inside the bundle.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of matching results to return. Default 5.",
                        "default": 5,
                    }
                },
                "required": ["bundle_id", "query"],
            },
        },
        group="documents",
        prompt_summary="Search parsed uploaded documents for relevant sections or chunks before reading larger spans, then follow any recommended section or chunk read hints when they are present.",
        progress_builder=_docs_search_progress,
        handler_method="_docs_search",
        read_only=True,
        specialist_agent_id=_DOCS_AGENT_ID,
    ),
    ToolSpec(
        name="docs_read",
        api_definition={
            "name": "docs_read",
            "description": (
                "Read parsed document content from the canonical document.md surface by full-document windows, sections, page ranges, slide ranges, chunk_ids, or anchor windows. "
                "This is the full-document read/export surface for parsed docs; there is intentionally no separate docs.export_full_md tool. "
                "Use read_kind=document with offset_chars and next_offset_chars to walk the source of truth sequentially when you need complete coverage. "
                "In multi-document bundles, provide doc_id once you know which parsed document you want to inspect."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {
                        "type": "string",
                        "description": "Parsed bundle ID shown in uploaded document metadata, such as bundle_docs_001.",
                    },
                    "doc_id": {
                        "type": "string",
                        "description": "Optional doc_id. Required when the parsed bundle contains multiple documents.",
                    },
                    "read_kind": {
                        "type": "string",
                        "description": "How to read the parsed bundle: document, section, page_range, slide_range, chunk_ids, or markdown_window. Use document for the canonical full-document markdown surface.",
                        "enum": ["document", "section", "page_range", "slide_range", "chunk_ids", "markdown_window"],
                    },
                    "section_id": {
                        "type": "string",
                        "description": "Optional section_id to load a semantically coherent section.",
                    },
                    "chunk_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional exact chunk IDs to load after docs_search.",
                    },
                    "start_page": {
                        "type": "integer",
                        "description": "Start page number when read_kind=page_range.",
                    },
                    "end_page": {
                        "type": "integer",
                        "description": "End page number when read_kind=page_range.",
                    },
                    "start_slide": {
                        "type": "integer",
                        "description": "Start slide number when read_kind=slide_range.",
                    },
                    "end_slide": {
                        "type": "integer",
                        "description": "End slide number when read_kind=slide_range.",
                    },
                    "anchor_id": {
                        "type": "string",
                        "description": "Section, chunk, page, slide, figure, or table anchor ID when read_kind=markdown_window.",
                    },
                    "offset_chars": {
                        "type": "integer",
                        "description": "Start offset into canonical document.md when reading the full document sequentially.",
                    },
                    "before_chars": {
                        "type": "integer",
                        "description": "Characters before the anchor when read_kind=markdown_window.",
                    },
                    "after_chars": {
                        "type": "integer",
                        "description": "Characters after the anchor when read_kind=markdown_window.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum number of characters to return. For long full-document reads, keep walking document.md with next_offset_chars until done.",
                        "default": 5000,
                    }
                },
                "required": ["bundle_id"],
            },
        },
        group="documents",
        prompt_summary="Read parsed uploaded documents from canonical document.md. Use read_kind=document as the intentional full-document surface, then walk it by sequential windows, sections, page ranges, slide ranges, exact chunk IDs, or anchor windows without pretending the whole file is already in context.",
        progress_builder=_docs_read_progress,
        handler_method="_docs_read",
        read_only=True,
        specialist_agent_id=_DOCS_AGENT_ID,
    ),
    ToolSpec(
        name="docs_fetch_asset",
        api_definition={
            "name": "docs_fetch_asset",
            "description": (
                "Fetch exact parsed sidecar asset metadata from a document bundle, such as a figure image, inline visual description, table markdown, or generated page or slide image."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {
                        "type": "string",
                        "description": "Parsed bundle ID shown in uploaded document metadata.",
                    },
                    "asset_id": {
                        "type": "string",
                        "description": "Exact asset_id returned by docs_browse on tables, figures, pages, or assets.",
                    },
                    "doc_id": {
                        "type": "string",
                        "description": "Optional doc_id when the bundle contains multiple documents and you already know the exact document.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum amount of text content to inline for text-like assets. Default 5000.",
                        "default": 5000,
                    }
                },
                "required": ["bundle_id", "asset_id"],
            },
        },
        group="documents",
        prompt_summary="Fetch an exact parsed sidecar asset when you need table markdown, figure metadata, or generated page-image references from a parsed bundle.",
        progress_builder=_docs_fetch_asset_progress,
        handler_method="_docs_fetch_asset",
        read_only=True,
        specialist_agent_id=_DOCS_AGENT_ID,
    ),
    ToolSpec(
        name="docs_reinspect_asset",
        api_definition={
            "name": "docs_reinspect_asset",
            "description": (
                "Ask the docs parser to visually reinspect one exact parsed image asset such as a chart, diagram, screenshot, page image, or slide image. "
                "Use this when inline descriptions are not enough and exact visual evidence matters."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {
                        "type": "string",
                        "description": "Parsed bundle ID shown in uploaded document metadata.",
                    },
                    "asset_id": {
                        "type": "string",
                        "description": "Exact image asset_id returned by docs_browse or docs_fetch_asset.",
                    },
                    "doc_id": {
                        "type": "string",
                        "description": "Optional doc_id when the bundle contains multiple documents and you already know the exact document.",
                    },
                    "question": {
                        "type": "string",
                        "description": "Optional focused question for the visual reinspection, such as 'What are the chart axes and trends?' or 'What does this slide layout emphasize?'.",
                    }
                },
                "required": ["bundle_id", "asset_id"],
            },
        },
        group="documents",
        prompt_summary="Use this when you need an exact visual read of one parsed chart, diagram, screenshot, page image, or slide image. This is the document-system-owned way to inspect an asset; do not pretend docs_fetch_asset alone means you visually inspected the image.",
        progress_builder=_docs_reinspect_asset_progress,
        handler_method="_docs_reinspect_asset",
        read_only=True,
        specialist_agent_id=_DOCS_AGENT_ID,
    ),
    ToolSpec(
        name="sheets_browse",
        api_definition={
            "name": "sheets_browse",
            "description": (
                "Browse a parsed spreadsheet bundle (tabular.parse_bundle output) without loading full tables. "
                "Use bundle_id from uploaded spreadsheet metadata after parsing."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string", "description": "Tabular bundle_id from spreadsheet parse metadata."},
                },
                "required": ["bundle_id"],
            },
        },
        group="spreadsheets",
        prompt_summary="List spreadsheet workbooks and handles for a parsed tabular bundle before schema/preview/query.",
        progress_builder=_sheets_browse_progress,
        handler_method="_sheets_browse",
        read_only=True,
        specialist_agent_id=_TABULAR_AGENT_ID,
    ),
    ToolSpec(
        name="sheets_schema",
        api_definition={
            "name": "sheets_schema",
            "description": "Return typed schema rows from sheet_catalog for one workbook artifact.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string"},
                    "artifact_id": {"type": "string", "description": "Workbook artifact_id from the parsed bundle."},
                    "sheet_id": {
                        "type": "string",
                        "description": "Optional filter to one sheet_id; must match ^[A-Za-z0-9_]{1,80}$ when set.",
                        "pattern": "^[A-Za-z0-9_]{1,80}$",
                    },
                },
                "required": ["bundle_id", "artifact_id"],
            },
        },
        group="spreadsheets",
        prompt_summary="Inspect column names, inferred types, and header detection for spreadsheet sheets.",
        progress_builder=_sheets_schema_progress,
        handler_method="_sheets_schema",
        read_only=True,
        specialist_agent_id=_TABULAR_AGENT_ID,
    ),
    ToolSpec(
        name="sheets_preview",
        api_definition={
            "name": "sheets_preview",
            "description": "Return bounded preview markdown for one sheet.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string"},
                    "artifact_id": {"type": "string"},
                    "sheet_id": {
                        "type": "string",
                        "description": "Must match ^[A-Za-z0-9_]{1,80}$.",
                        "pattern": "^[A-Za-z0-9_]{1,80}$",
                    },
                },
                "required": ["bundle_id", "artifact_id", "sheet_id"],
            },
        },
        group="spreadsheets",
        prompt_summary="Read a small markdown preview of one parsed sheet.",
        progress_builder=_sheets_preview_progress,
        handler_method="_sheets_preview",
        read_only=True,
        specialist_agent_id=_TABULAR_AGENT_ID,
    ),
    ToolSpec(
        name="sheets_query",
        api_definition={
            "name": "sheets_query",
            "description": (
                "Run a read-only SELECT against the DuckDB bundle. Views are named s_<sheet_id>. "
                "Only SELECT statements; no writes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string"},
                    "artifact_id": {"type": "string"},
                    "sql": {"type": "string", "description": "Single SELECT statement."},
                },
                "required": ["bundle_id", "artifact_id", "sql"],
            },
        },
        group="spreadsheets",
        prompt_summary="Deterministic SQL over parsed spreadsheet Parquet-backed views.",
        progress_builder=_sheets_query_progress,
        handler_method="_sheets_query",
        read_only=True,
        specialist_agent_id=_TABULAR_AGENT_ID,
    ),
    ToolSpec(
        name="sheets_export",
        api_definition={
            "name": "sheets_export",
            "description": "Export a SELECT result to CSV or Parquet under the bundle exports folder.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string"},
                    "artifact_id": {"type": "string"},
                    "sql": {"type": "string"},
                    "format": {"type": "string", "description": "parquet or csv", "enum": ["parquet", "csv"]},
                },
                "required": ["bundle_id", "artifact_id", "sql"],
            },
        },
        group="spreadsheets",
        prompt_summary="Persist a derived table from a SELECT query into spreadsheet artifacts.",
        progress_builder=_sheets_export_progress,
        handler_method="_sheets_export",
        read_only=False,
        specialist_agent_id=_TABULAR_AGENT_ID,
    ),
    ToolSpec(
        name="sheets_export_sheet",
        api_definition={
            "name": "sheets_export_sheet",
            "description": "Export one existing parsed sheet directly to CSV, XLSX, or Parquet without writing SQL.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string"},
                    "artifact_id": {"type": "string"},
                    "sheet_id": {
                        "type": "string",
                        "description": "Existing sheet_id to export; must match ^[A-Za-z0-9_]{1,80}$.",
                        "pattern": "^[A-Za-z0-9_]{1,80}$",
                    },
                    "format": {
                        "type": "string",
                        "description": "csv, xlsx, or parquet",
                        "enum": ["csv", "xlsx", "parquet"],
                    },
                },
                "required": ["bundle_id", "artifact_id", "sheet_id"],
            },
        },
        group="spreadsheets",
        prompt_summary="Export one existing sheet from a parsed spreadsheet bundle to a downloadable file.",
        progress_builder=_sheets_export_sheet_progress,
        handler_method="_sheets_export_sheet",
        read_only=False,
        specialist_agent_id=_TABULAR_AGENT_ID,
    ),
    ToolSpec(
        name="sheets_create_workbook",
        api_definition={
            "name": "sheets_create_workbook",
            "description": (
                "Create a brand-new workbook bundle from structured sheets/rows and return a downloadable .xlsx file. "
                "Use this when the spreadsheet does not already exist as an uploaded bundle."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Optional output workbook filename; .xlsx is added automatically if missing.",
                    },
                    "bundle_label": {
                        "type": "string",
                        "description": "Optional human label for the created workbook bundle.",
                    },
                    "sheets": {
                        "type": "array",
                        "minItems": 1,
                        "description": "One or more sheet definitions for the new workbook.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "sheet_id": {
                                    "type": "string",
                                    "description": "Optional stable id; if provided must match ^[A-Za-z0-9_]{1,80}$.",
                                    "pattern": "^[A-Za-z0-9_]{1,80}$",
                                },
                                "display_name": {
                                    "type": "string",
                                    "description": "Human sheet name shown in Excel and the bundle catalog.",
                                },
                                "columns": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional explicit column order, especially useful for empty sheets.",
                                },
                                "rows": {
                                    "type": "array",
                                    "description": "Rows as objects or arrays. Empty rows are allowed when columns are provided.",
                                    "items": {"anyOf": [{"type": "object"}, {"type": "array"}]},
                                },
                            },
                            "required": ["rows"],
                        },
                    },
                },
                "required": ["sheets"],
            },
        },
        group="spreadsheets",
        prompt_summary="Create a new workbook bundle from structured data and return a downloadable .xlsx artifact.",
        progress_builder=_sheets_create_workbook_progress,
        handler_method="_sheets_create_workbook",
        read_only=False,
        specialist_agent_id=_TABULAR_AGENT_ID,
    ),
    ToolSpec(
        name="sheets_create_sheet",
        api_definition={
            "name": "sheets_create_sheet",
            "description": (
                "Create an empty sheet (Parquet + DuckDB view) inside an existing parsed workbook bundle. "
                "Updates sheet_catalog and workbook metadata so browse/schema/preview stay consistent."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string"},
                    "artifact_id": {"type": "string", "description": "Workbook artifact_id from the parsed bundle."},
                    "sheet_id": {
                        "type": "string",
                        "description": "Stable id; becomes DuckDB view s_<sheet_id>. Must match ^[A-Za-z0-9_]{1,80}$.",
                        "pattern": "^[A-Za-z0-9_]{1,80}$",
                    },
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Column names for the empty sheet.",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Optional human label shown in sheet catalog; defaults to sheet_id.",
                    },
                },
                "required": ["bundle_id", "artifact_id", "sheet_id", "columns"],
            },
        },
        group="spreadsheets",
        prompt_summary="Add an empty derived sheet to a parsed spreadsheet bundle with explicit columns.",
        progress_builder=_sheets_create_sheet_progress,
        handler_method="_sheets_create_sheet",
        read_only=False,
        specialist_agent_id=_TABULAR_AGENT_ID,
    ),
    ToolSpec(
        name="sheets_reason",
        api_definition={
            "name": "sheets_reason",
            "description": (
                "Delegate a **single goal** to the tabular specialist's internal agentic workflow: it inspects "
                "catalog/preview, plans one SQL or bounded-Python step, runs deterministic DuckDB or the COSMIC "
                "sandbox (codes/ + executions/), and returns a summary. Prefer granular sheets_schema / sheets_query "
                "when you already know the SQL; use this for exploratory or multi-step reasoning without raw workbook bytes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string"},
                    "artifact_id": {"type": "string", "description": "Workbook artifact_id from the parsed bundle."},
                    "goal": {
                        "type": "string",
                        "description": "What to find or compute over the bundle (natural language).",
                    },
                    "allow_python": {
                        "type": "boolean",
                        "description": "Allow bounded sandbox Python under the bundle (default true). Set false for SQL-only.",
                        "default": True,
                    },
                },
                "required": ["bundle_id", "artifact_id", "goal"],
            },
        },
        group="spreadsheets",
        prompt_summary="Internal tabular specialist reasoning (plan + execute + summarize); use when granular tools are insufficient.",
        progress_builder=_sheets_reason_progress,
        handler_method="_sheets_reason",
        read_only=False,
        specialist_agent_id=_TABULAR_AGENT_ID,
    ),
    ToolSpec(
        name="firecrawl_scrape",
        api_definition={
            "name": "firecrawl_scrape",
            "description": (
                "Use the Firecrawl specialist agent to scrape a live web page into robust formats such as markdown, html, links, images, or screenshot metadata. "
                "Prefer this over plain web_fetch when you need page rendering resilience or structured scrape outputs."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Single page URL to scrape.",
                    },
                    "formats": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Requested formats such as markdown, html, rawHtml, links, images, or screenshot.",
                    },
                    "only_main_content": {
                        "type": "boolean",
                        "description": "Prefer the main page content over boilerplate. Default true.",
                        "default": True,
                    },
                    "wait_for_ms": {
                        "type": "integer",
                        "description": "Optional page wait delay in milliseconds before scraping.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Optional scrape timeout in milliseconds.",
                    },
                    "max_age_ms": {
                        "type": "integer",
                        "description": "Optional cache max-age in milliseconds.",
                    },
                    "include_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional include-tags hint for the page scrape.",
                    },
                    "exclude_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional exclude-tags hint for the page scrape.",
                    },
                    "mobile": {
                        "type": "boolean",
                        "description": "Use a mobile user-agent when scraping. Default false.",
                        "default": False,
                    },
                    "proxy": {
                        "type": "string",
                        "description": "Optional Firecrawl proxy tier: auto, basic, or enhanced.",
                        "enum": ["auto", "basic", "enhanced"],
                    },
                },
                "required": ["url"],
            },
        },
        group="research",
        prompt_summary="Robust page scrape via the Firecrawl specialist agent when plain fetch is not enough and you need clean formats or rendered content artifacts.",
        progress_builder=_firecrawl_scrape_progress,
        handler_method="_firecrawl_scrape",
        exposed_to_model=False,
        specialist_agent_id=_FIRECRAWL_AGENT_ID,
    ),
    ToolSpec(
        name="firecrawl_extract",
        api_definition={
            "name": "firecrawl_extract",
            "description": (
                "Use the Firecrawl specialist agent to extract structured data from one or more URLs. "
                "Use this for schema-shaped research outputs, list building, and reliable multi-page extraction."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One or more page URLs to extract from.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Extraction instruction describing the fields or structure you need.",
                    },
                    "schema": {
                        "type": "object",
                        "description": "Optional JSON schema for the extracted structured output.",
                    },
                    "show_sources": {
                        "type": "boolean",
                        "description": "Whether to return source references when available.",
                        "default": False,
                    },
                    "enable_web_search": {
                        "type": "boolean",
                        "description": "Allow Firecrawl web search assist during extraction if needed.",
                        "default": False,
                    },
                    "only_main_content": {
                        "type": "boolean",
                        "description": "Prefer main page content during Firecrawl scrapeOptions. Default true.",
                        "default": True,
                    },
                    "wait_for_ms": {
                        "type": "integer",
                        "description": "Optional page wait delay in milliseconds before extraction scraping.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Optional extraction timeout in milliseconds.",
                    },
                    "max_age_ms": {
                        "type": "integer",
                        "description": "Optional cache max-age in milliseconds.",
                    },
                },
                "required": ["urls", "prompt"],
            },
        },
        group="research",
        prompt_summary="Structured extraction via the Firecrawl specialist agent for schema-shaped outputs, list building, and multi-page research tasks.",
        progress_builder=_firecrawl_extract_progress,
        handler_method="_firecrawl_extract",
        exposed_to_model=False,
        specialist_agent_id=_FIRECRAWL_AGENT_ID,
    ),
    ToolSpec(
        name="firecrawl_agent",
        api_definition={
            "name": "firecrawl_agent",
            "description": (
                "Run the Firecrawl autonomous AI agent to search, navigate, and extract data from the web given a natural-language prompt. "
                "Use this only when simpler firecrawl_scrape or firecrawl_extract have failed or are clearly insufficient — "
                "for example when the right URLs are unknown, multi-page interaction is needed, or a complex extraction requires autonomous navigation. "
                "Do not default to this mode; prefer firecrawl_scrape and firecrawl_extract first."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Natural-language description of the data to find and extract. Be specific and descriptive.",
                    },
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional seed URLs to focus the agent on. When omitted the agent discovers URLs autonomously.",
                    },
                    "schema": {
                        "type": "object",
                        "description": "Optional JSON schema describing the desired structured output shape.",
                    },
                },
                "required": ["prompt"],
            },
        },
        group="research",
        prompt_summary=(
            "Autonomous Firecrawl AI agent for complex extractions when scrape/extract are insufficient. "
            "Searches, navigates, and extracts autonomously. Use only as a fallback."
        ),
        progress_builder=_firecrawl_agent_progress,
        handler_method="_firecrawl_agent",
        exposed_to_model=False,
        specialist_agent_id=_FIRECRAWL_AGENT_ID,
    ),
    ToolSpec(
        name="firecrawl_recall_session",
        api_definition={
            "name": "firecrawl_recall_session",
            "description": (
                "Read the Firecrawl agent's private run ledger for a prior session. Use this when you need exact recall of what the Firecrawl specialist already scraped or extracted."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID whose Firecrawl runs should be recalled.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of Firecrawl runs to return. Default 10.",
                        "default": 10,
                    },
                },
                "required": ["session_id"],
            },
        },
        group="research",
        prompt_summary="Exact recall of the Firecrawl agent's private session ledger so you can reuse or inspect prior Firecrawl work.",
        progress_builder=_firecrawl_recall_progress,
        handler_method="_firecrawl_recall_session",
        read_only=True,
        exposed_to_model=False,
        specialist_agent_id=_FIRECRAWL_AGENT_ID,
    ),
    ToolSpec(
        name="x_search",
        api_definition={
            "name": "x_search",
            "description": (
                "Use the X/Twitter Search specialist agent to search X deeply through xAI Grok's native x_search capability. "
                "Use this for current sentiment, reaction analysis, handle-scoped X search, and evidence-grounded X briefings."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The X/Twitter search request.",
                    },
                    "analysis_goal": {
                        "type": "string",
                        "description": "Optional extra instruction about what the search should determine or compare.",
                    },
                    "allowed_x_handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional X handles to include, without leading @.",
                    },
                    "excluded_x_handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional X handles to exclude, without leading @.",
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Optional ISO-8601 lower bound for X search.",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "Optional ISO-8601 upper bound for X search.",
                    },
                    "enable_image_understanding": {
                        "type": "boolean",
                        "description": "Enable image understanding in X search when visual tweet content matters.",
                        "default": False,
                    },
                    "enable_video_understanding": {
                        "type": "boolean",
                        "description": "Enable video understanding in X search when video tweet content matters.",
                        "default": False,
                    },
                    "max_posts": {
                        "type": "integer",
                        "description": "Maximum number of notable posts to include in the structured result. Maximum 30. Default 30.",
                        "maximum": 30,
                        "default": 30,
                    },
                },
                "required": ["query"],
            },
        },
        group="research",
        prompt_summary="Deep X/Twitter search through the X specialist agent when the request depends on current X reactions, handle-scoped search, or evidence-grounded social search.",
        progress_builder=_x_search_progress,
        handler_method="_x_search",
        exposed_to_model=False,
        specialist_agent_id=_X_SEARCH_AGENT_ID,
    ),
    ToolSpec(
        name="x_recall_session",
        api_definition={
            "name": "x_recall_session",
            "description": (
                "Read the X/Twitter Search agent's private run ledger for a prior session. Use this when you need exact recall of what the X specialist already searched."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID whose X-search runs should be recalled.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of X-search runs to return. Default 10.",
                        "default": 10,
                    },
                },
                "required": ["session_id"],
            },
        },
        group="research",
        prompt_summary="Exact recall of the X/Twitter Search agent's private session ledger so you can reuse or inspect prior X-search work.",
        progress_builder=_x_recall_progress,
        handler_method="_x_recall_session",
        read_only=True,
        exposed_to_model=False,
        specialist_agent_id=_X_SEARCH_AGENT_ID,
    ),
    ToolSpec(
        name="memory_search",
        api_definition={
            "name": "memory_search",
            "description": (
                "Actively search the shared memory system for prior facts, project details, task summaries, session summaries, "
                "artifact pointers, and related entities. Returns raw memory hits, graph signals, episodes, and the search plan when available."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query describing what to look for in memory.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of memory hits to return. Default 5.",
                        "default": 5,
                    },
                    "token_budget": {
                        "type": "integer",
                        "description": "Approximate result-size budget for the active search response. Default 3000.",
                        "default": 3000,
                    },
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional memory-kind filter such as core_fact, user_data, session_summary, task_summary, agent_note, transcript, or artifact_pointer.",
                    },
                    "seed_memory_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional memory IDs to anchor active recall and graph expansion around known memories.",
                    },
                    "seed_entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional canonical entity names to seed active recall around known people, projects, files, or topics.",
                    },
                    "max_hops": {
                        "type": "integer",
                        "description": "Maximum graph-expansion depth for active recall. Default 2.",
                        "default": 2,
                    },
                    "include_diagnostics": {
                        "type": "boolean",
                        "description": "Include search diagnostics when you need to inspect how memory recall was assembled.",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        },
        group="memory",
        prompt_summary="Active shared-memory search. Use this for durable memories, prior tasks, summaries, and artifact pointers.",
        progress_builder=_memory_search_progress,
        handler_method="_memory_search",
        read_only=True,
    ),
    ToolSpec(
        name="memory_fetch",
        api_definition={
            "name": "memory_fetch",
            "description": (
                "Read the full canonical shared-memory block for a specific memory_id. Use this after memory_search "
                "or when you already know the exact memory ID you need to inspect in detail."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The exact memory_id to load, such as mem_task_abc123.",
                    },
                },
                "required": ["memory_id"],
            },
        },
        group="memory",
        prompt_summary="Load the full canonical memory block for a specific memory_id when a search hit needs deeper inspection.",
        progress_builder=_memory_fetch_progress,
        handler_method="_memory_fetch",
        read_only=True,
    ),
    ToolSpec(
        name="memory_write",
        api_definition={
            "name": "memory_write",
            "description": (
                "Write durable shared memory that should remain retrievable later. Use kind=user_data for personal/project facts "
                "that should be searchable later, or kind=agent_note for implementation notes, reasoning summaries, and notable work context. "
                "Do not use this for always-on core profile facts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The memory content to store as a clear standalone statement or short note.",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Shared memory kind. Use user_data for durable facts/preferences/goals. Use agent_note for durable implementation or project notes.",
                        "enum": ["user_data", "agent_note"],
                        "default": "agent_note",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title summarizing the memory. If omitted, one will be derived.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional search tags for the saved memory.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional structured metadata to store alongside the memory.",
                    },
                },
                "required": ["content"],
            },
        },
        group="memory",
        prompt_summary="Persist retrievable shared memory. Prefer user_data for durable user/project facts and agent_note for notable implementation context.",
        progress_builder=_memory_write_progress,
        handler_method="_memory_write",
    ),
    ToolSpec(
        name="heartbeat_notes",
        api_definition={
            "name": "heartbeat_notes",
            "description": (
                "Read or maintain COSMIC's private heartbeat notes markdown file. Use this during heartbeat turns as a compact self-scratchpad "
                "for watchpoints, future checks, project improvement ideas, and stale notes to remove. Keep notes short and do not expose them verbatim."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Operation to perform.",
                        "enum": ["read", "append", "replace", "remove", "clear"],
                        "default": "read",
                    },
                    "content": {
                        "type": "string",
                        "description": "Markdown content for append or replace. For append, write only the new concise note(s). For replace, provide the complete notes body.",
                    },
                    "match": {
                        "type": "string",
                        "description": "Exact text to remove when action=remove.",
                    },
                },
            },
        },
        group="memory",
        prompt_summary="Private heartbeat self-notes markdown. Use during heartbeat turns to read, append, replace, or remove compact watchpoints across beats without polluting chat history.",
        progress_builder=_heartbeat_notes_progress,
        handler_method="_heartbeat_notes",
    ),
    ToolSpec(
        name="memory_write_core_fact",
        api_definition={
            "name": "memory_write_core_fact",
            "description": (
                "Write a stable always-on core fact or standing preference that should surface proactively in the core_fact block. "
                "Use canonical_key when you are updating an established field such as response style, relationship, or identity fact. "
                "Only use this for relationship or identity facts when the user explicitly confirmed the fact or the trusted source directly names it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The durable fact or standing preference to store.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title summarizing the fact. If omitted, one will be derived.",
                    },
                    "canonical_key": {
                        "type": "string",
                        "description": "Stable key for superseding future updates, such as preferences.response_style or relationships.spouse.",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Relative ordering priority inside the core_fact block. Default 100.",
                        "default": 100,
                    },
                    "always_include": {
                        "type": "boolean",
                        "description": "Whether this fact should remain eligible for always-on inclusion in the core_fact block. Default true.",
                        "default": True,
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional search tags for the saved fact.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional structured metadata to store alongside the core fact.",
                    },
                },
                "required": ["fact"],
            },
        },
        group="memory",
        prompt_summary="Persist a stable always-on core fact or standing preference. Prefer this over memory_write when the fact should proactively shape future context, and only use it for relationship or identity facts when they are explicitly confirmed or directly named by the trusted source.",
        progress_builder=_memory_core_fact_progress,
        handler_method="_memory_write_core_fact",
    ),
    ToolSpec(
        name="session_state",
        api_definition={
            "name": "session_state",
            "description": (
                "Read the deterministic session-state packet for a session, including compacted summary, active working set, carry-forward packet, and compaction metadata."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID like sess_20260315. If omitted, the current session is used.",
                    },
                },
            },
        },
        group="history",
        prompt_summary="Deterministic session state for exact continuity, working-set, and carry-forward inspection.",
        progress_builder=_session_state_progress,
        handler_method="_session_state",
        read_only=True,
    ),
    ToolSpec(
        name="session_turns",
        api_definition={
            "name": "session_turns",
            "description": (
                "Read the compact turn ledger for a session. Use this when you need a structured summary of what happened across prior turns."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID like sess_20260315. If omitted, the current session is used.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of turn-ledger entries to return. Default 20.",
                        "default": 20,
                    },
                },
            },
        },
        group="history",
        prompt_summary="Structured turn-ledger summaries for a session. Good for decision/history review without loading raw text.",
        progress_builder=_session_turns_progress,
        handler_method="_session_turns",
        read_only=True,
    ),
    ToolSpec(
        name="session_history",
        api_definition={
            "name": "session_history",
            "description": (
                "Read raw session messages directly from the canonical session store. Use this when exact prior turn text matters."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID like sess_20260315. If omitted, the current session is used.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Zero-based message offset for paging through raw history.",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of raw messages to return. Default 20.",
                        "default": 20,
                    },
                },
            },
        },
        group="history",
        prompt_summary="Paged raw session messages from the canonical session store when exact earlier wording matters.",
        progress_builder=_session_history_progress,
        handler_method="_session_history",
        read_only=True,
    ),
    ToolSpec(
        name="task_notebook",
        api_definition={
            "name": "task_notebook",
            "description": (
                "Read the compact task notebook for a task. Use this to recover the task goal, current state, key findings, open questions, and next actions."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID like tsk_abc123. If omitted, the current task is used.",
                    },
                },
            },
        },
        group="history",
        prompt_summary="Compact per-task state and progress notebook for exact task recovery and resumption.",
        progress_builder=_task_notebook_progress,
        handler_method="_task_notebook",
        read_only=True,
    ),
    ToolSpec(
        name="session_revisit",
        api_definition={
            "name": "session_revisit",
            "description": (
                "Build a deterministic revisit bundle for a session. This combines session state, turn-ledger entries, a raw-history tail, and optionally a task notebook or specific request turn."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID like sess_20260315. If omitted, the current session is used.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional task ID whose notebook should be included in the revisit payload.",
                    },
                    "request_id": {
                        "type": "string",
                        "description": "Optional request ID whose turn-ledger entry should be included.",
                    },
                    "turn_limit": {
                        "type": "integer",
                        "description": "Number of turn-ledger entries to include. Default 8.",
                        "default": 8,
                    },
                    "raw_history_limit": {
                        "type": "integer",
                        "description": "Number of raw history messages to include. Default 12.",
                        "default": 12,
                    },
                },
            },
        },
        group="history",
        prompt_summary="Preferred exact-history recovery bundle. Use this before broad memory search when the exact earlier context matters.",
        progress_builder=_session_revisit_progress,
        handler_method="_session_revisit",
        read_only=True,
    ),
    ToolSpec(
        name="create_event_automation",
        api_definition={
            "name": "create_event_automation",
            "description": (
                "Create or update a standing event automation. Use this when the user gives an instruction like "
                "'when X happens, do Y', especially for inbound Gmail, future webhooks, calendar changes, files, or other external events. "
                "Store the user's exact instruction plus a compact structured condition/action; unresolved people are allowed and can be resolved on future events."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "string",
                        "description": "Optional existing automation id when updating a rule.",
                    },
                    "event_type": {
                        "type": "string",
                        "description": "Event family such as gmail.inbound. Default gmail.inbound for user-owned Gmail standing instructions.",
                        "default": "gmail.inbound",
                    },
                    "label": {
                        "type": "string",
                        "description": "Short human-readable name for the standing automation.",
                    },
                    "raw_instruction": {
                        "type": "string",
                        "description": "The user's exact standing instruction in natural language.",
                    },
                    "condition": {
                        "type": "object",
                        "description": (
                            "Compact match condition. For Gmail, useful keys include person_ref, sender_email, sender_domain, "
                            "subject_contains, body_contains, topic, keywords, has_attachment, and resolution_mode. "
                            "Do not require exact email addresses when the user named a person naturally."
                        ),
                    },
                    "action": {
                        "type": "object",
                        "description": (
                            "What COSMIC should do after a match. Prefer type=orchestrator_task with a high-level goal. "
                            "Do not encode provider-specific hardcoded steps when the orchestrator should reason from context."
                        ),
                    },
                    "approval_policy": {
                        "type": "object",
                        "description": (
                            "Approval boundaries. Typical policy: drafts/local prep allowed; sending, external sharing, deletion, purchases, or irreversible changes require approval."
                        ),
                    },
                },
                "required": ["raw_instruction"],
            },
        },
        group="automations",
        prompt_summary=(
            "Create standing event automations for 'when X happens, do Y' instructions. "
            "Use for Gmail/webhook/calendar/file triggers instead of merely saving a memory."
        ),
        progress_builder=_create_event_automation_progress,
        handler_method="_create_event_automation",
    ),
    ToolSpec(
        name="list_event_automations",
        api_definition={
            "name": "list_event_automations",
            "description": "List active or all standing event automations.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "description": "Optional event type filter such as gmail.inbound.",
                    },
                    "status": {
                        "type": "string",
                        "description": "Status filter: active, paused, inactive, or all. Default active.",
                        "default": "active",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of automations to return. Default 50.",
                        "default": 50,
                    },
                },
            },
        },
        group="automations",
        prompt_summary="Inspect existing standing event automations before changing or deleting them.",
        progress_builder=lambda _tool_input: "Checking event automations...",
        handler_method="_list_event_automations",
        read_only=True,
    ),
    ToolSpec(
        name="delete_event_automation",
        api_definition={
            "name": "delete_event_automation",
            "description": "Deactivate a standing event automation by id.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "automation_id": {
                        "type": "string",
                        "description": "Automation id returned by create_event_automation or list_event_automations.",
                    },
                },
                "required": ["automation_id"],
            },
        },
        group="automations",
        prompt_summary="Deactivate a standing event automation when the user cancels or no longer wants it.",
        progress_builder=_delete_event_automation_progress,
        handler_method="_delete_event_automation",
    ),
    ToolSpec(
        name="create_reminder",
        api_definition={
            "name": "create_reminder",
            "description": (
                "Create a scheduled reminder or recurring cron job. Default delivery is the current channel. "
                "Use delivery_target only when the user explicitly wants a different channel, and include "
                "context_summary for recurring or long-delay reminders so the future run still has the right context."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Human-readable label for the reminder. Shown in schedule listings.",
                    },
                    "cron_expression": {
                        "type": "string",
                        "description": "Standard 5-field cron expression in the user's local timezone.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The message or instruction sent back to the orchestrator when the schedule fires.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Optional explicit IANA timezone such as America/Chicago. Omit this unless the user explicitly asked for a different timezone than their current local timezone.",
                    },
                    "delivery_target": {
                        "type": "string",
                        "description": (
                            "Optional logical delivery target. Prefer simple values like desktop, whatsapp, telegram, or current. "
                            "Use this only when the user explicitly wants a different delivery channel than the one they are using now."
                        ),
                    },
                    "delivery_channel": {
                        "type": "string",
                        "description": (
                            "Optional exact concrete channel such as whatsapp:+12153079021. "
                            "Use this only as an escape hatch when you must pin an exact channel."
                        ),
                    },
                    "context_summary": {
                        "type": "string",
                        "description": (
                            "Optional durable reminder context. Include a concise summary for recurring or long-delay reminders "
                            "describing the baseline, comparison goal, or reason the future run exists."
                        ),
                    },
                    "one_shot": {
                        "type": "boolean",
                        "description": "If true, the reminder fires once and then becomes inactive.",
                        "default": True,
                    },
                },
                "required": ["label", "cron_expression", "prompt"],
            },
        },
        group="scheduling",
        prompt_summary="Create one-shot reminders or recurring scheduled tasks. Default delivery is the current channel; use delivery_target only when explicitly requested, and add context_summary for long-delay or recurring jobs.",
        progress_builder=_create_reminder_progress,
        handler_method="_create_reminder",
    ),
    ToolSpec(
        name="list_reminders",
        api_definition={
            "name": "list_reminders",
            "description": "List all active reminders and cron jobs.",
            "input_schema": {
                "type": "object",
                "properties": {},
            },
        },
        group="scheduling",
        prompt_summary="Inspect the current reminder and schedule list.",
        progress_builder=lambda _tool_input: "Checking your reminders...",
        handler_method="_list_reminders",
        read_only=True,
    ),
    ToolSpec(
        name="delete_reminder",
        api_definition={
            "name": "delete_reminder",
            "description": "Delete a scheduled reminder or cron job by its ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "cron_id": {
                        "type": "string",
                        "description": "The ID of the reminder or cron job to delete.",
                    },
                },
                "required": ["cron_id"],
            },
        },
        group="scheduling",
        prompt_summary="Delete an existing reminder or scheduled task.",
        progress_builder=_delete_reminder_progress,
        handler_method="_delete_reminder",
    ),
)

_TOOL_BY_NAME = {spec.name: spec for spec in _MODEL_TOOL_SPECS}
_GROUP_ORDER = (
    "web",
    "research",
    "specialists",
    "code",
    "artifacts",
    "documents",
    "spreadsheets",
    "planning",
    "memory",
    "history",
    "automations",
    "scheduling",
)
_GROUP_TITLES = {
    "web": "Web",
    "research": "Research",
    "specialists": "Specialists",
    "code": "Code Execution",
    "artifacts": "Artifacts",
    "documents": "Documents",
    "spreadsheets": "Spreadsheets",
    "planning": "Planning & Wishlist",
    "memory": "Memory",
    "history": "History",
    "automations": "Event Automations",
    "scheduling": "Scheduling",
}


def get_model_tool_definitions(featured_agent_ids: set[str] | None = None) -> list[dict[str, Any]]:
    return [spec.to_definition() for spec in _MODEL_TOOL_SPECS if spec.is_visible_to_model(featured_agent_ids)]


def get_local_tool_definitions(featured_agent_ids: set[str] | None = None) -> list[dict[str, Any]]:
    return [
        spec.to_definition()
        for spec in _MODEL_TOOL_SPECS
        if spec.is_local and spec.is_visible_to_model(featured_agent_ids)
    ]


def get_tool_spec(name: str) -> ToolSpec | None:
    return _TOOL_BY_NAME.get(str(name or "").strip())


def get_local_tool_spec(name: str) -> ToolSpec | None:
    spec = get_tool_spec(name)
    if spec is None or not spec.is_local:
        return None
    return spec


def get_parallel_safe_local_tool_names() -> frozenset[str]:
    return frozenset(spec.name for spec in _MODEL_TOOL_SPECS if spec.is_local and spec.read_only)


def build_tool_progress_message(tool_name: str, tool_input: dict[str, Any]) -> str:
    spec = get_tool_spec(tool_name)
    if spec is None or spec.progress_builder is None:
        return f"Using tool: {tool_name}..."
    return spec.progress_builder(tool_input)


def build_tool_prompt_catalog(featured_agent_ids: set[str] | None = None) -> str:
    grouped: dict[str, list[str]] = {}
    for spec in _MODEL_TOOL_SPECS:
        if not spec.is_visible_to_model(featured_agent_ids) or not spec.prompt_summary:
            continue
        grouped.setdefault(spec.group, []).append(f"- `{spec.name}`: {spec.prompt_summary}")

    lines = ["## Available Tools", ""]
    for group in _GROUP_ORDER:
        items = grouped.get(group)
        if not items:
            continue
        lines.append(f"### {_GROUP_TITLES[group]}")
        lines.extend(items)
        lines.append("")
    return "\n".join(lines).strip()


def get_tool_registry_snapshot(featured_agent_ids: set[str] | None = None) -> dict[str, Any]:
    return {
        "model_tools": [spec.name for spec in _MODEL_TOOL_SPECS if spec.is_visible_to_model(featured_agent_ids)],
        "local_tools": [
            spec.name
            for spec in _MODEL_TOOL_SPECS
            if spec.is_local and spec.is_visible_to_model(featured_agent_ids)
        ],
        "server_tools": [
            spec.name
            for spec in _MODEL_TOOL_SPECS
            if not spec.is_local and spec.is_visible_to_model(featured_agent_ids)
        ],
        "read_only_local_tools": sorted(
            spec.name
            for spec in _MODEL_TOOL_SPECS
            if spec.is_local and spec.read_only and spec.is_visible_to_model(featured_agent_ids)
        ),
    }
