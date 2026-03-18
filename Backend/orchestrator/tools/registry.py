from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable


ProgressBuilder = Callable[[dict[str, Any]], str]


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

    @property
    def is_local(self) -> bool:
        return self.handler_method is not None

    def to_definition(self) -> dict[str, Any]:
        return deepcopy(self.api_definition)


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


def _wishlist_search_progress(tool_input: dict[str, Any]) -> str:
    query = str(tool_input.get("query") or "").strip()
    return f"Checking COSMIC's capability wishlist for: {query}" if query else "Checking COSMIC's capability wishlist..."


def _wishlist_capture_progress(tool_input: dict[str, Any]) -> str:
    title = str(tool_input.get("title") or "").strip()
    return f"Recording capability gap: {title}" if title else "Recording a capability gap for COSMIC..."


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


def _firecrawl_recall_progress(tool_input: dict[str, Any]) -> str:
    session_id = str(tool_input.get("session_id") or "").strip()
    return f"Reviewing prior Firecrawl runs for {session_id}..." if session_id else "Reviewing prior Firecrawl runs..."


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
                "and you need synthesis, comparison, or a more thorough answer."
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
        prompt_summary="Deep synthesized research across multiple sources when a quick web lookup is not enough.",
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
                        "description": "Structured payload for the specialist intent. Keep it minimal and match the schema hints returned by agent_catalog_search.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional exact agent_id if you want a specific registered specialist version.",
                    },
                    "wait_timeout_sec": {
                        "type": "number",
                        "description": "Optional override for how long to wait before returning an in-progress result.",
                    },
                },
                "required": ["intent", "input"],
            },
        },
        group="specialists",
        prompt_summary="Delegate specialist work by exact intent after discovery. Prefer this over carrying agent-specific tools in your tool list.",
        progress_builder=_delegate_to_agent_progress,
        handler_method="_delegate_to_agent",
    ),
    ToolSpec(
        name="cosmics_capability_wishlist_search",
        api_definition={
            "name": "cosmics_capability_wishlist_search",
            "description": (
                "Search COSMIC's capability wishlist for existing or similar missing capabilities. "
                "Use this when you need to inspect previously recorded gaps, roadmap items, or similar wishes."
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
        prompt_summary="Search the canonical COSMIC capability wishlist for similar missing capabilities or roadmap items.",
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
                "The backend automatically searches for similar entries, deduplicates, and may update an existing item."
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
        prompt_summary="Capture a real missing capability when you notice COSMIC would materially help the user better if it had that capability already.",
        progress_builder=_wishlist_capture_progress,
        handler_method="_cosmics_capability_wishlist_capture",
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
        name="memory_write_core_fact",
        api_definition={
            "name": "memory_write_core_fact",
            "description": (
                "Write a stable always-on core fact or standing preference that should surface proactively in the core_fact block. "
                "Use canonical_key when you are updating an established field such as response style, relationship, or identity fact."
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
        prompt_summary="Persist a stable always-on core fact or standing preference. Prefer this over memory_write when the fact should proactively shape future context.",
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
_GROUP_ORDER = ("web", "research", "specialists", "planning", "memory", "history", "scheduling")
_GROUP_TITLES = {
    "web": "Web",
    "research": "Research",
    "specialists": "Specialists",
    "planning": "Planning & Wishlist",
    "memory": "Memory",
    "history": "History",
    "scheduling": "Scheduling",
}


def get_model_tool_definitions() -> list[dict[str, Any]]:
    return [spec.to_definition() for spec in _MODEL_TOOL_SPECS if spec.exposed_to_model]


def get_local_tool_definitions() -> list[dict[str, Any]]:
    return [spec.to_definition() for spec in _MODEL_TOOL_SPECS if spec.is_local and spec.exposed_to_model]


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


def build_tool_prompt_catalog() -> str:
    grouped: dict[str, list[str]] = {}
    for spec in _MODEL_TOOL_SPECS:
        if not spec.exposed_to_model or not spec.prompt_summary:
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


def get_tool_registry_snapshot() -> dict[str, Any]:
    return {
        "model_tools": [spec.name for spec in _MODEL_TOOL_SPECS if spec.exposed_to_model],
        "local_tools": [spec.name for spec in _MODEL_TOOL_SPECS if spec.is_local and spec.exposed_to_model],
        "server_tools": [spec.name for spec in _MODEL_TOOL_SPECS if not spec.is_local and spec.exposed_to_model],
        "read_only_local_tools": sorted(
            spec.name
            for spec in _MODEL_TOOL_SPECS
            if spec.is_local and spec.read_only and spec.exposed_to_model
        ),
    }
