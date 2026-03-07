#!/usr/bin/env python3
"""
Meeting bridge for Cosmic-OS.

Provides:
- Live Deepgram transcription (microphone)
- Claude Haiku 4.5 (Anthropic) meeting orchestration (summary, cues, nudge, action items)
- Ask-about-meeting answers
- SQLite persistence via resources/database.py

Groq GPT-OSS code is commented out but retained for fallback.
"""

from __future__ import annotations

import asyncio
import atexit
import audioop
import hashlib
import json
import os
import re
import string
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import requests
import websockets
from dotenv import load_dotenv

from database import db

load_dotenv()

try:
    import pyaudio
    HAS_PYAUDIO = True
except ImportError:
    pyaudio = None
    HAS_PYAUDIO = False

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    httpx = None
    HAS_HTTPX = False

DEBUG = True
TARGET_SR = 16000
FRAME_MS = 100
SAMPLES_PER_FRAME = TARGET_SR * FRAME_MS // 1000  # 1600 samples = 3200 bytes at 100ms
CHANNELS = 1

DG_WS = "wss://api.deepgram.com/v1/listen"
DG_MODEL = os.getenv("DG_MODEL", "nova-3")

# Claude Haiku 4.5 — minimal thinking for speed (https://docs.anthropic.com/en/api/messages)
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MEETING_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_BASE = "https://api.anthropic.com/v1"

# Groq (commented out — retained for fallback)
# GROQ_MODEL = os.getenv("GROQ_MEETING_MODEL", "openai/gpt-oss-120b")

DG_WS_PING_INTERVAL = 20
DG_WS_PING_TIMEOUT = 10
DG_WS_MAX_SIZE = 2 ** 20
DG_CONNECT_TIMEOUT = 15.0
DG_MAX_RETRIES = 3
DG_KEEPALIVE_INTERVAL = 5
ANTHROPIC_HTTP2_ENABLED = True
ANTHROPIC_MAX_CONNECTIONS = 8
ANTHROPIC_KEEPALIVE_EXPIRY = 30.0
ANTHROPIC_CONNECT_TIMEOUT = 10.0
ANTHROPIC_WRITE_TIMEOUT = 20.0
ANTHROPIC_POOL_TIMEOUT = 5.0
ANTHROPIC_READ_TIMEOUT = 30.0
ANTHROPIC_STREAM_READ_TIMEOUT = 60.0
DEFAULT_MEETING_SETTINGS = {
    "name_on_call": "User",
    "mic_sensitivity": 55,
    "update_interval_sec": 1.0,
}

UPDATE_INTERVAL_MIN = 1.0
UPDATE_INTERVAL_MAX = 5.0
NUDGE_REPEAT_COOLDOWN_SEC = 18.0

_ANTHROPIC_HTTP_LOCAL = threading.local()
_ANTHROPIC_HTTP_CLIENTS: List[Any] = []
_ANTHROPIC_HTTP_CLIENTS_LOCK = threading.Lock()


def dlog(*args: Any) -> None:
    if DEBUG:
        print("[meeting]", *args, file=sys.stderr, flush=True)


def _build_anthropic_http2_client() -> Optional[Any]:
    if not HAS_HTTPX:
        return None
    return httpx.Client(
        http2=ANTHROPIC_HTTP2_ENABLED,
        limits=httpx.Limits(
            max_connections=ANTHROPIC_MAX_CONNECTIONS,
            max_keepalive_connections=ANTHROPIC_MAX_CONNECTIONS,
            keepalive_expiry=ANTHROPIC_KEEPALIVE_EXPIRY,
        ),
        timeout=httpx.Timeout(
            ANTHROPIC_READ_TIMEOUT,
            connect=ANTHROPIC_CONNECT_TIMEOUT,
            write=ANTHROPIC_WRITE_TIMEOUT,
            pool=ANTHROPIC_POOL_TIMEOUT,
        ),
    )


def _get_anthropic_http2_client() -> Optional[Any]:
    client = getattr(_ANTHROPIC_HTTP_LOCAL, "client", None)
    if client is not None and not getattr(client, "is_closed", False):
        return client

    client = _build_anthropic_http2_client()
    if client is None:
        return None

    _ANTHROPIC_HTTP_LOCAL.client = client
    with _ANTHROPIC_HTTP_CLIENTS_LOCK:
        _ANTHROPIC_HTTP_CLIENTS.append(client)
    return client


def _close_anthropic_http2_clients() -> None:
    with _ANTHROPIC_HTTP_CLIENTS_LOCK:
        clients = list(_ANTHROPIC_HTTP_CLIENTS)
        _ANTHROPIC_HTTP_CLIENTS.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


atexit.register(_close_anthropic_http2_clients)


def emit(tag: str, payload: Dict[str, Any]) -> None:
    print(f"<<{tag}>>{json.dumps(payload, ensure_ascii=False)}<<END>>", flush=True)


def emit_status(status: str, **extra: Any) -> None:
    payload = {"status": status, "timestamp": time.time()}
    payload.update(extra)
    emit("MEETING_STATUS", payload)


def clamp_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(round(float(value)))
    except Exception:
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def clamp_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return minimum
    return max(minimum, min(maximum, parsed))


def get_meeting_settings() -> Dict[str, Any]:
    raw = db.get_all_meeting_settings()
    return {
        "name_on_call": str(raw.get("name_on_call") or DEFAULT_MEETING_SETTINGS["name_on_call"]).strip() or DEFAULT_MEETING_SETTINGS["name_on_call"],
        "mic_sensitivity": clamp_int(raw.get("mic_sensitivity", DEFAULT_MEETING_SETTINGS["mic_sensitivity"]), 0, 100),
        "update_interval_sec": clamp_float(
            raw.get("update_interval_sec", DEFAULT_MEETING_SETTINGS["update_interval_sec"]),
            UPDATE_INTERVAL_MIN,
            UPDATE_INTERVAL_MAX,
        ),
    }


def save_meeting_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    current = get_meeting_settings()

    if "name_on_call" in patch:
        current["name_on_call"] = (
            str(patch.get("name_on_call") or "").strip()
            or DEFAULT_MEETING_SETTINGS["name_on_call"]
        )
        db.set_meeting_setting("name_on_call", current["name_on_call"])

    if "mic_sensitivity" in patch:
        current["mic_sensitivity"] = clamp_int(
            patch.get("mic_sensitivity"),
            0,
            100,
        )
        db.set_meeting_setting("mic_sensitivity", current["mic_sensitivity"])

    if "update_interval_sec" in patch:
        current["update_interval_sec"] = clamp_float(
            patch.get("update_interval_sec"),
            UPDATE_INTERVAL_MIN,
            UPDATE_INTERVAL_MAX,
        )
        db.set_meeting_setting("update_interval_sec", str(current["update_interval_sec"]))

    return get_meeting_settings()


def normalize_summary(text: str) -> str:
    if not text:
        return ""
    return text.lower().translate(str.maketrans("", "", string.punctuation)).strip()


def unique_ordered(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        normalized = normalize_summary(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(item.strip())
    return out


def _summary_tokens(text: str) -> List[str]:
    base = normalize_summary(text)
    if not base:
        return []
    return [tok for tok in re.findall(r"[a-z0-9]+", base) if len(tok) > 2]


def summary_similarity(a: str, b: str) -> float:
    ta = set(_summary_tokens(a))
    tb = set(_summary_tokens(b))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    if union == 0:
        return 0.0
    return inter / union


def compact_summary_lines(
    lines: List[str],
    dedupe_similarity: float = 0.82,
) -> List[str]:
    compacted: List[str] = []
    seen_exact = set()

    for raw in lines:
        text = str(raw or "").strip()
        if not text:
            continue

        norm = normalize_summary(text)
        if not norm:
            continue
        if norm in seen_exact:
            continue

        if compacted:
            prev = compacted[-1]
            sim = summary_similarity(prev, text)
            if sim >= dedupe_similarity:
                # Dynamic refinement: keep whichever summary is richer.
                if len(norm) >= int(len(normalize_summary(prev)) * 0.9):
                    compacted[-1] = text
                    seen_exact.add(norm)
                continue

        compacted.append(text)
        seen_exact.add(norm)

    return compacted


def get_service_key(service: str, env_name: str) -> str:
    key = db.get_api_key(service)
    if key:
        return key
    return os.getenv(env_name, "").strip()


def extract_delimited_block(text: str, tag: str) -> str:
    pattern = rf"{tag}_START\[(.*?)\]{tag}_END"
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

STRICT_TIER_A_PROMPT = """
You are Cosmic, a real-time AI co-pilot for {user_name} during a live meeting.

## IDENTITY RULE (CRITICAL)
In the transcript, the speaker tagged "[Me]" IS {user_name}. They are the SAME person.
"Me" = {user_name}. Always write "{user_name}" in your output, never "Me" or "Speaker Me".
Diarization is imperfect — sometimes {user_name}'s speech is tagged as "Speaker N". Use context to figure out who is actually speaking.

## OUTPUT FORMAT
Return EXACTLY these blocks in order, nothing else. No markdown fences, no extra text.

TRANSCRIPT_FIXES_START[
<segment_id>::<corrected text>
<segment_id>::<corrected text>
]TRANSCRIPT_FIXES_END
SUMMARY_START[one sentence summarizing what just happened, ≤ 20 words]SUMMARY_END
CUES_START[<question 1>; <question 2>; <question 3>;]CUES_END
NUDGE_START[<proactive help for {user_name}, or empty>]NUDGE_END
ACTION_ITEMS_START[<task-1>; <task-2>; or empty]ACTION_ITEMS_END

## WHAT EACH BLOCK DOES

**TRANSCRIPT_FIXES**: Surgical transcript repair for the recent numbered lines.
- Only include lines that actually need correction. If none need changes, leave the block empty.
- Format each fix as `<segment_id>::<corrected text>` on its own line.
- Keep the same speaker meaning. Do NOT change speaker labels. Do NOT mention segment ids anywhere except this block.
- Fix clear STT mistakes, homophones, entity names, product names, APIs, acronyms, dates, and unfinished phrasing when the intended wording is highly inferable from context.
- Do not invent facts. If uncertain, skip the fix.
- Before writing SUMMARY, CUES, NUDGE, or ACTION_ITEMS, first mentally rewrite the transcript into its corrected intended meaning.
- Use that corrected interpretation as canonical for all downstream output. Never generate cues or nudges from the mistaken raw wording if the intended wording is clear.
- If a later fragment clearly disambiguates an earlier fragment, TRANSCRIPT_FIXES MUST repair the earlier fragment too.
- Back-propagate clarified entities/terms across the recent window. Example: later "Cursor, the coding IDE" means earlier "ID" should be repaired to "IDE"; later "Composer AI model" means earlier "compose area model" should be repaired to "Composer AI model" when clearly the same reference.
- Short ambiguous fragments from the same speaker are especially important to repair once the intended term becomes clear a few lines later.
- Resolve acronym/entity consistency across the whole recent window before you output anything.
- If the same speaker repeats one acronym/name/term multiple times and one nearby variant looks like an STT error, repair the outlier to the dominant intended term.
- Example: if several nearby lines say "YC", "Y Combinator", or "YC student startup pack", then an isolated "OEC" in the same question thread is almost certainly an STT error and should be repaired to "YC", not treated as a different organization.

**SUMMARY**: Factual description of what was discussed. Not an answer, not analysis — a summary.
- BAD: "A singleton ensures one instance." (that's an answer)
- GOOD: "Speaker 1 asked about the singleton pattern."

**CUES** (Suggested questions): 3 short questions {user_name} can click to ask the AI.
These are questions FOR the AI about the meeting content. Not instructions to the user.
Think: "What would {user_name} want to look up or understand right now?"
- Scan for explicit questions, confusions, or new terms in the transcript by any speaker so that we can cover for diarization errors.
- Identify entities: tools, APIs, libraries, people, metrics, dates, decisions
- Turn them into natural questions: "What is Triton-V5?", "Explain the singleton pattern", "Steps to finish Q3 report"
- Keep all cues anchored to the corrected canonical entity/topic. Do not split one clarified topic into multiple unrelated entities because of STT noise.
- If one acronym/entity is clearly dominant in the recent window, all relevant cues should use that entity consistently.

**NUDGE** (Real-time coaching): The most valuable block. Pack in whatever helps {user_name} right now:
{nudge_behavior_block}
- **What to say now**: A line {user_name} can say verbatim (e.g. "You could say: …" or "Try: …").
- **Answers**: If someone on the call asked a question, give {user_name} the answer to say out loud.
- **Help & support**: Definitions, key facts, trade-offs, or context for entities/tools/APIs/decisions being discussed.
- Combine these when relevant (e.g. short answer + suggested line, or context + what to say).
- Priority order for NUDGE:
  1. Answer the most recent explicit question.
  2. If that question was repeated or rephrased, treat it as especially important and answer that question directly.
  3. Only if there is no clear explicit question, give background/help/support.
- Do not replace a direct answer with adjacent background context.
- If someone asks "How are they different?", "Why is it better?", "What changed?", or any other comparison/follow-up question, answer that comparison directly.
- Resolve pronouns like "they", "it", "this", "that" from nearby context and answer the intended question, not a related side topic.
- If multiple questions appear close together, answer the latest unresolved question first. Use CUES for the secondary questions.
- Keep NUDGE ultra concise: HARD LIMIT <= 150 characters total, including spaces and punctuation.
- Generate the NUDGE under 150 characters on the first pass. Do not draft a longer answer mentally and then compress it.
- Prefer one tight sentence. Use two very short sentences only if still under 150 characters.
- Drop filler, qualifiers, throat-clearing, repeated context, names already obvious from context, and polite framing.
- If needed, answer only the single most useful fact instead of a fuller explanation.
- Use compressed phrasing. Prefer: "San Francisco." over "The team is based in San Francisco."
- Speed matters more than completeness. Choose the shortest useful answer that helps right now.
- If `previous_nudge` already answered the broader topic and the latest speech is just a follow-up, answer only the missing delta. Do not restate what the previous nudge already covered.
- Example: if the previous nudge already explained what Cursor is and who built it, and the latest question is "Where is this team from?", answer only "San Francisco."
- If the latest conversation contains ANY explicit or implicit question, confusion, request for explanation, or newly mentioned entity/acronym/tool/API/person/metric/decision that would benefit from quick context, NUDGE MUST be non-empty.
- Leave NUDGE empty only when the latest speech is truly filler/social chatter or too fragmentary to infer anything useful.
- **Fragment completion**: Speech often arrives in chunks. If {user_name} said "What is… doing right now?" and then in the next line named a person/thing (e.g. "Who is actor Ajit Kumar?"), treat it as ONE question: "What is actor Ajit Kumar doing right now?". In the nudge, use the actual name/entity they said — never suggest they "complete it with [name]" when they already specified who or what in the following line.
- Keep nudge answers concise: not too much info. Prefer the direct answer; cut filler and long explanations.



**ACTION_ITEMS**: ONLY when someone explicitly commits to doing something ("I'll send it", "Let's schedule that").
Never from questions, speculation, or ideas. If unsure, leave empty.

## PRIORITIES
- Focus on the LATEST transcript (after "--- LATEST PART OF CONVERSATION ---"). The older parts and summary_history give context.
- **Correct transcription mistakes intelligently**: Live transcripts often have wrong words due to transcription errors (homophones, mishears). Use context to infer what was actually said (e.g. "poodle" near "model" → "model", "Triton V" in a tech discussion → "Triton V5"). Apply this when writing SUMMARY, CUES, and NUDGE — interpret the speaker’s intent, not the raw text.
- Prefer the specialized term that best fits the surrounding meeting domain rather than a generic everyday word when context supports it.
- Use TRANSCRIPT_FIXES to push those repairs back into the transcript store when confidence is high, but do not wait for a second pass. The same response must already reflect the corrected understanding.
- When the speaker later restates or clarifies their own question, treat that later clarified wording as canonical and repair the earlier fragmented lines to match it where appropriate.
- Repeated self-clarifications beat isolated mistranscriptions. A single odd acronym should not override several nearby mentions of the same clarified entity.
- **Combine consecutive fragments from the same speaker**: If the user asked "What is X?" and then in the next turn named a person/thing (e.g. "Who is actor Ajit Kumar?"), infer the full question (e.g. "What is actor Ajit Kumar doing right now?") and reflect that in NUDGE/CUES — do not suggest a generic "complete it: What is [name] doing right now?".
- Keep everything short. Speed matters more than thoroughness.
- If someone asks something answerable from general knowledge or meeting context, use NUDGE to answer it immediately instead of only turning it into a cue.
- If the transcript is just small talk, filler, or someone still mid-sentence with no question/entity/decision signal — return empty CUES and empty NUDGE.

{live_web_rules_block}

{custom_instructions_block}
""".strip()


STRICT_EXPLAINER_PROMPT = """
You are Cosmic, answering a question for {user_name} about their live meeting.

## IDENTITY RULE (CRITICAL)
- In transcripts, "Me" IS {user_name}. They are the same person.
- In your answer text, NEVER refer to {user_name} in the third person. Always address the reader as "you".
  BAD: "If Praveen is discussing...", "For {user_name}'s meeting". GOOD: "If you're discussing...", "In your meeting".

## RULES
1. Output valid Markdown. No delimiter tags (SUMMARY_START, etc).
2. **Be direct**: Lead with a short, direct answer (1–2 sentences). Avoid long intros or extra detail. Only add optional sections (`## What this means for this meeting`, `## Suggested line to say now`, `## Details`) if they clearly add value — otherwise skip them.
3. Use `summary_history` as the main durable context and `recent_transcript` for the freshest details. Use `meeting_profile` for metadata.
   If summary is thin, answer from `recent_transcript` and `meeting_profile` directly. Don't say context is missing.
   Fix transcription errors using context clues and your knowledge of likely entities/terms (infer what was actually said).
   If the meeting contains an explicit or implicit question, answer the intended question even if the transcript wording is messy.
4. **Web search (you have it):** Use the browser search tool whenever the question implies
   they want up-to-date or external information. That includes: "latest", "current", "recent",
   "today", "now", "updates", "status", "what's new", "any news", "current price/version",
   or anything about live events, APIs, docs, or the outside world. When in doubt and it
   could be time-sensitive, search.
   - Never output tool-call markup (`<function_calls>`, `<invoke>`, raw JSON tool calls).
     Use tools silently and return only the final user-facing answer.
5. End with `## References` — bullet links for external claims, or `- Meeting summary context only.`
6. Keep answers concise: not too much info. Prefer the direct answer; cut filler and long explanations.

{custom_instructions_block}
""".strip()


STRICT_EXPLAINER_PROMPT_NO_WEB = """
You are Cosmic, answering a question for {user_name} about their live meeting.

## IDENTITY RULE (CRITICAL)
- In transcripts, "Me" IS {user_name}. They are the same person.
- In your answer text, NEVER refer to {user_name} in the third person. Always address the reader as "you".
  BAD: "If Praveen is discussing...", "For {user_name}'s meeting". GOOD: "If you're discussing...", "In your meeting".

## RULES
1. Output valid Markdown. No delimiter tags (SUMMARY_START, etc).
2. **Be direct**: Lead with a short, direct answer (1–2 sentences). Avoid long intros or extra detail. Only add optional sections (`## What this means for this meeting`, `## Suggested line to say now`, `## Details`) if they clearly add value — otherwise skip them.
3. Use `summary_history` as the main durable context and `recent_transcript` for the freshest details. Use `meeting_profile` for metadata.
   If summary is thin, answer from `recent_transcript` and `meeting_profile` directly. Don't say context is missing.
   Fix transcription errors using context clues and your knowledge of likely entities/terms (infer what was actually said).
   If the meeting contains an explicit or implicit question, answer the intended question even if the transcript wording is messy.
4. No web browsing available. If live data is needed, note the uncertainty.
5. End with `## References` — bullet links if any, or `- Meeting summary context only.`
6. Keep answers concise: not too much info. Prefer the direct answer; cut filler and long explanations.

{custom_instructions_block}
""".strip()


NUDGE_REPAIR_PROMPT = """
You are Cosmic, repairing a missed nudge for {user_name} during a live meeting.

Return ONLY the nudge text. No tags. No markdown bullets unless needed.

Rules:
- If the latest transcript contains an explicit or implicit question, answer it directly.
- Prioritize the most recent explicit question over adjacent background/context.
- If a question was repeated or rephrased, answer that repeated question directly.
- For comparison questions like "How are they different?", resolve the entities from nearby context and answer the comparison itself.
- If a tool, API, acronym, person, company, metric, or decision is mentioned and likely needs clarification, define it briefly.
- If useful, append one short line {user_name} could say now.
- Fix obvious STT mistakes using context and likely entity names.
- Keep it concrete and helpful. HARD LIMIT <= 150 characters total, including spaces and punctuation.
- Generate the output under 150 characters on the first pass. Do not draft a longer answer and then compress it.
- Prefer one tight sentence. Use two very short sentences only if still under 150 characters.
- Drop filler and keep only the highest-value fact or line.
- Speed matters more than completeness. Choose the shortest useful answer.
- If `previous_nudge` already answered the broader topic and the latest speech is a follow-up, answer only the new missing detail.
- If there is truly nothing useful to say, return an empty string.

{live_web_rules_block}

{custom_instructions_block}
""".strip()


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

@dataclass
class MeetingUpdate:
    transcript_fixes: Dict[int, str]
    summary: str
    cues: List[str]
    nudge: str
    action_items: List[str]


class MeetingRuntime:
    def __init__(
        self,
        meeting_id: str,
        title: str,
        user_name: str,
        goal: str = "",
        custom_instructions: str = "",
        mic_sensitivity: int = DEFAULT_MEETING_SETTINGS["mic_sensitivity"],
        update_interval_sec: float = DEFAULT_MEETING_SETTINGS["update_interval_sec"],
        web_search_enabled: bool = False,
    ) -> None:
        self.meeting_id = meeting_id
        self.title = title
        self.user_name = user_name or "User"
        self.goal = goal or ""
        self.custom_instructions = custom_instructions or ""
        self.mic_sensitivity = clamp_int(mic_sensitivity, 0, 100)
        self.update_interval_sec = clamp_float(update_interval_sec, UPDATE_INTERVAL_MIN, UPDATE_INTERVAL_MAX)
        self.web_search_enabled = bool(web_search_enabled)

        self.anthropic_key = get_service_key("anthropic", "ANTHROPIC_API_KEY")
        self.deepgram_key = get_service_key("deepgram", "DEEPGRAM_API_KEY")
        # self.groq_key = get_service_key("groq", "GROQ_API_KEY")  # retained for fallback

        self.transcript: deque = deque(maxlen=4000)
        self.summaries: deque = deque(maxlen=200)
        self.action_items: List[str] = []

        self.is_running = False
        self.is_paused = False
        self.start_ts = time.time()
        self.total_pause_duration = 0.0
        self.pause_started_at: Optional[float] = None

        self.last_hash: Optional[str] = None
        self.last_summary = ""
        self.last_nudge = ""
        self.last_nudge_time: float = 0.0
        self.last_update_time: float = 0.0
        self._pending_generation = False
        self.pending_interim: Optional[Dict[str, Any]] = None

        self.new_transcript_event = threading.Event()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

        self.processor_thread: Optional[threading.Thread] = None
        self.audio_thread: Optional[threading.Thread] = None
        self.audio_loop: Optional[asyncio.AbstractEventLoop] = None
        self.audio_stream = None
        self.websocket_conn = None
        self.noise_floor_rms = 120.0
        self.calibration_frames = 0
        self.speech_hold_frames = 0

    def current_meeting_time(self) -> float:
        elapsed = time.time() - self.start_ts
        return max(0.0, elapsed - self.total_pause_duration)

    def apply_settings(self, settings: Dict[str, Any]) -> None:
        self.user_name = (
            str(settings.get("name_on_call") or "").strip()
            or DEFAULT_MEETING_SETTINGS["name_on_call"]
        )
        self.mic_sensitivity = clamp_int(
            settings.get("mic_sensitivity", self.mic_sensitivity),
            0,
            100,
        )
        if "update_interval_sec" in settings:
            self.update_interval_sec = clamp_float(
                settings["update_interval_sec"],
                UPDATE_INTERVAL_MIN,
                UPDATE_INTERVAL_MAX,
            )

    def set_web_search_enabled(self, enabled: Any) -> None:
        self.web_search_enabled = bool(enabled)

    def _build_instructions_block(self) -> str:
        parts: List[str] = []
        if self.goal.strip():
            parts.append(f"## MEETING GOAL (from {self.user_name})\n{self.goal.strip()}")
        if self.custom_instructions.strip():
            parts.append(
                f"## USER INSTRUCTIONS (highest priority — override other rules if conflicting)\n"
                f"{self.custom_instructions.strip()}"
            )
        if not parts:
            return ""
        return "\n\n".join(parts) + "\n"

    def _build_nudge_behavior_block(self) -> str:
        goal = (self.goal or "").strip().lower()
        custom = (self.custom_instructions or "").strip().lower()
        combined = f"{goal} {custom}"

        wants_questions = any(phrase in combined for phrase in [
            "suggest question", "questions i can ask", "ask question",
            "help me ask", "what to ask", "give me question",
            "questions to ask", "prompt me with question",
            "interview question", "prepare question",
        ])
        is_presentation = any(phrase in combined for phrase in [
            "presentation", "lecture", "class", "seminar",
            "webinar", "demo", "attending",
        ])

        if wants_questions:
            return (
                "NUDGE BEHAVIOR: {user_name} wants suggested questions to ask. Include:\n"
                "- **What to say now**: A question {user_name} can ask verbatim (e.g. \"Try asking: …\").\n"
                "- **Support**: Brief context so {user_name} knows why to ask it or what to listen for.\n"
                "- **Instant answers**: If someone already asked a question on the call, answer it first, then suggest the follow-up.\n"
                "React to what was just said; if a claim or term came up, suggest a follow-up question + one line of context."
            ).format(user_name=self.user_name)
        elif is_presentation:
            return (
                "NUDGE BEHAVIOR: {user_name} is attending. Include:\n"
                "- **Help & support**: Definitions, acronyms, key facts about tools/concepts being presented.\n"
                "- **What to say now** (optional): A question or comment {user_name} could make if they want to engage.\n"
                "- **Instant answers**: If the presenter or another attendee asks something answerable, give {user_name} the short answer.\n"
                "Prioritize clarity on unfamiliar terms; add a suggested line to say when it would add value."
            ).format(user_name=self.user_name)
        else:
            return (
                "NUDGE BEHAVIOR: Full real-time coaching. Include any that fit:\n"
                "- **What to say now**: A line {user_name} can say verbatim (e.g. \"You could say: …\").\n"
                "- **Answers**: If someone asked a question on the call, give {user_name} the answer to say.\n"
                "- **Help & support**: Key facts, trade-offs, or definitions for what's being discussed (tools, APIs, decisions, metrics).\n"
                "Combine what to say + answer or what to say + support when it helps. Be concrete and actionable."
            ).format(user_name=self.user_name)

    def _build_live_web_rules_block(self) -> str:
        if not self.web_search_enabled:
            return ""
        return (
            "## WEB SEARCH\n"
            "- Web search is enabled for this live update.\n"
            "- Use it silently only when the latest transcript clearly needs current or external facts.\n"
            "- Good triggers: current leaders, recent changes, product/company facts, docs, pricing, locations, dates, outside-world verification.\n"
            "- Do not search for obvious common knowledge or context already clear from the meeting.\n"
            "- Never output tool-call markup; still return only the required blocks."
        )

    @staticmethod
    def _build_local_timestamp_block() -> str:
        now = datetime.now().astimezone()
        tz_name = now.tzname() or "Local"
        return (
            "User local timestamp context (from the user's machine):\n"
            f"User local timestamp (ISO): {now.isoformat(timespec='seconds')}\n"
            f"User local date: {now.strftime('%Y-%m-%d')}\n"
            f"User local time: {now.strftime('%H:%M:%S')}\n"
            f"User local timezone: {tz_name} (UTC{now.strftime('%z')})"
        )

    def _build_meeting_profile_block(self) -> str:
        lines = [
            "Meeting profile (canonical metadata):",
            f"Meeting title: {(self.title or '').strip() or 'Not specified'}",
            f"Meeting goal: {(self.goal or '').strip() or 'Not specified'}",
            f"User on call: {(self.user_name or '').strip() or 'User'}",
        ]
        custom = (self.custom_instructions or "").strip()
        if custom:
            lines.append("Custom instructions:")
            lines.append(custom[:3000])
        else:
            lines.append("Custom instructions: Not specified")
        return "\n".join(lines)

    @staticmethod
    def _normalize_transcript_text(text: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        cleaned = re.sub(r"\s+([,.;:?!])", r"\1", cleaned)
        return cleaned

    @staticmethod
    def _parse_transcript_fixes(block: str) -> Dict[int, str]:
        fixes: Dict[int, str] = {}
        if not block or not block.strip():
            return fixes

        for raw_line in block.splitlines():
            line = raw_line.strip().lstrip("-").strip()
            if not line or "::" not in line:
                continue
            segment_ref, corrected_text = line.split("::", 1)
            match = re.search(r"\d+", segment_ref)
            if not match:
                continue
            fixed_text = MeetingRuntime._normalize_transcript_text(corrected_text)
            if not fixed_text:
                continue
            fixes[int(match.group(0))] = fixed_text
        return fixes

    @staticmethod
    def _extract_latest_section(transcript_window: str) -> str:
        marker = "--- LATEST PART OF CONVERSATION (PRIORITIZE THIS) ---"
        if marker in transcript_window:
            return transcript_window.split(marker, 1)[1].strip()
        return transcript_window.strip()

    @staticmethod
    def _segment_signature(speaker: str, text: str) -> str:
        return f"{str(speaker or '').strip().lower()}::{normalize_summary(text)}"

    def _apply_transcript_fixes(self, fixes: Dict[int, str]) -> bool:
        if not fixes:
            return False

        payloads_to_emit: List[Dict[str, Any]] = []
        with self.lock:
            by_id = {
                int(seg["segment_id"]): seg
                for seg in self.transcript
                if isinstance(seg, dict) and seg.get("segment_id") is not None
            }
            for segment_id, corrected_text in fixes.items():
                seg = by_id.get(int(segment_id))
                if not seg:
                    continue

                current_text = self._normalize_transcript_text(seg.get("text", ""))
                normalized_fix = self._normalize_transcript_text(corrected_text)
                if not normalized_fix or normalized_fix == current_text:
                    continue

                baseline_len = max(len(current_text), 1)
                if len(normalized_fix) > max(220, (baseline_len * 4) + 20):
                    continue

                seg["text"] = normalized_fix
                payloads_to_emit.append(dict(seg))

        for payload in payloads_to_emit:
            segment_id = payload.get("segment_id")
            if segment_id is None:
                continue
            try:
                db.update_meeting_transcript_text(segment_id, payload.get("text", ""))
            except Exception as exc:
                dlog("Transcript correction persist failed:", exc)
            payload["correction"] = True
            emit("MEETING_TRANSCRIPT", payload)

        return bool(payloads_to_emit)

    @staticmethod
    def _needs_nudge_repair(transcript_window: str, cues: Optional[List[str]] = None) -> bool:
        latest = MeetingRuntime._extract_latest_section(transcript_window)
        if not latest:
            return False
        if cues:
            return True
        if "?" in latest:
            return True
        return bool(re.search(
            r"(?i)\b("
            r"what|why|how|who|when|where|which|explain|define|meaning|means|"
            r"compare|difference|versus|vs\.?|latest|current|status|update|"
            r"unclear|confused|not sure|help me understand"
            r")\b",
            latest,
        ))

    def _build_answer_context(self) -> Dict[str, str]:
        live_summary_lines: List[str] = []
        if self.is_running:
            live_summary = self._summary_history(minutes_back=8, max_items=80)
            if live_summary.strip():
                live_summary_lines.extend([ln for ln in live_summary.splitlines() if ln.strip()])
        else:
            if self.summaries:
                for s in reversed(self.summaries):
                    c = s["content"] if isinstance(s, dict) else str(s)
                    if c.strip():
                        live_summary_lines.append(c.strip())

        context = db.get_meeting_context(self.meeting_id, transcript_limit=120, update_limit=500)
        persisted_summary_lines: List[str] = []
        for update in context.get("updates", []):
            summary = str(update.get("summary", "")).strip()
            if summary:
                persisted_summary_lines.append(summary)

        merged_summary_lines = compact_summary_lines([
            *persisted_summary_lines,
            *live_summary_lines,
        ])
        summary_text = "\n".join(merged_summary_lines[-220:])

        transcript_lines: List[str] = []
        for segment in context.get("transcripts", []):
            text = self._normalize_transcript_text(str(segment.get("text", "")).strip())
            if not text:
                continue
            speaker = str(segment.get("speaker", "Speaker")).strip() or "Speaker"
            transcript_lines.append(f"[{speaker}] {text}")
        transcript_text = "\n".join(transcript_lines[-120:])

        if not summary_text.strip():
            bootstrap_lines: List[str] = ["No rolling summary available yet."]
            if self.title.strip():
                bootstrap_lines.append(f"Meeting title: {self.title.strip()}")
            if self.goal.strip():
                bootstrap_lines.append(f"Meeting goal: {self.goal.strip()}")
            if self.user_name.strip():
                bootstrap_lines.append(f"User on call: {self.user_name.strip()}")
            if self.custom_instructions.strip():
                bootstrap_lines.append(
                    "Custom instructions:\n"
                    f"{self.custom_instructions.strip()[:1500]}"
                )
            summary_text = "\n".join(bootstrap_lines)

        return {
            "summary_text": summary_text,
            "transcript_text": transcript_text,
        }

    # ---- Parsing ----

    def _parse_structured_update(self, raw: str) -> Optional[MeetingUpdate]:
        if not raw or not raw.strip():
            return None

        formatted = self._reformat_citations(raw)

        transcript_fixes_text = extract_delimited_block(formatted, "TRANSCRIPT_FIXES")
        summary = extract_delimited_block(formatted, "SUMMARY")
        cues_text = extract_delimited_block(formatted, "CUES")
        nudge = extract_delimited_block(formatted, "NUDGE")
        action_items_text = extract_delimited_block(formatted, "ACTION_ITEMS")

        suggestions_text = extract_delimited_block(formatted, "SUGGESTIONS")
        if suggestions_text and not cues_text:
            cues_text = suggestions_text

        # Fallback parsing for near-miss formats like:
        # Summary: ...
        # Cues: ...
        # Nudge: ...
        # Action Items: ...
        if not summary:
            m = re.search(r"(?im)^\s*summary\s*[:\-]\s*(.+)$", formatted)
            if m:
                summary = m.group(1).strip()

        if not cues_text:
            m = re.search(
                r"(?is)\bcues?\s*[:\-]\s*(.+?)(?:\n\s*(?:nudge|action\s*items?)\s*[:\-]|$)",
                formatted,
            )
            if m:
                cues_text = m.group(1).strip().replace("\n", "; ")

        if not nudge:
            m = re.search(
                r"(?is)\bnudge\s*[:\-]\s*(.+?)(?:\n\s*(?:action\s*items?|summary|cues?)\s*[:\-]|$)",
                formatted,
            )
            if m:
                nudge = m.group(1).strip()

        if not action_items_text:
            m = re.search(r"(?is)\baction\s*items?\s*[:\-]\s*(.+)$", formatted)
            if m:
                action_items_text = m.group(1).strip().replace("\n", "; ")

        cues = [c.strip() for c in cues_text.split(";") if c.strip()]
        action_items = [a.strip() for a in action_items_text.split(";") if a.strip()]

        cues = unique_ordered(cues)[:3]
        action_items = unique_ordered(action_items)[:6]

        if not summary:
            return None

        return MeetingUpdate(
            transcript_fixes=self._parse_transcript_fixes(transcript_fixes_text),
            summary=summary,
            cues=cues,
            nudge=nudge,
            action_items=action_items,
        )

    @staticmethod
    def _reformat_citations(text: str) -> str:
        """Clean up any citation artifacts from tool use responses."""
        text = re.sub(r"\[\d+\]", "", text)
        return text.strip()

    @staticmethod
    def _strip_delimited_block(text: str, tag: str) -> str:
        pattern = rf"{tag}_START\[(.*?)\]{tag}_END"
        return re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL)

    @staticmethod
    def _anthropic_headers(api_key: str) -> Dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

    @staticmethod
    def _anthropic_timeout(stream: bool = False) -> Any:
        if HAS_HTTPX:
            return httpx.Timeout(
                ANTHROPIC_STREAM_READ_TIMEOUT if stream else ANTHROPIC_READ_TIMEOUT,
                connect=ANTHROPIC_CONNECT_TIMEOUT,
                write=ANTHROPIC_WRITE_TIMEOUT,
                pool=ANTHROPIC_POOL_TIMEOUT,
            )
        return ANTHROPIC_STREAM_READ_TIMEOUT if stream else ANTHROPIC_READ_TIMEOUT

    @staticmethod
    def _build_anthropic_payload(
        system_prompt: str,
        user_content: str,
        temperature: float,
        max_tokens: int,
        allow_web: bool,
        stream: bool = False,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
            "temperature": temperature,
            "thinking": {"type": "disabled"},
        }
        if stream:
            payload["stream"] = True
        if allow_web:
            payload["tools"] = [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 3,
            }]
        return payload

    @classmethod
    def _extract_anthropic_text_from_blocks(cls, content_blocks: List[Any]) -> Optional[str]:
        has_server_tool_use = any(
            isinstance(block, dict) and block.get("type") == "server_tool_use"
            for block in content_blocks
        )

        parts: List[str] = []
        saw_search_results = False
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "web_search_tool_result":
                saw_search_results = True
                continue
            if btype != "text":
                continue

            text = (block.get("text") or "").strip()
            if not text:
                continue

            # Skip search preambles when tool use happened before real results.
            if has_server_tool_use and not saw_search_results:
                continue
            parts.append(text)

        if not parts:
            for block in content_blocks:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                if re.match(r"(?is)^(i('| a)?ll|let me|i will)\s+search\b", text):
                    continue
                parts.append(text)

        if not parts:
            return None
        return cls._clean_anthropic_text("\n\n".join(parts))

    @classmethod
    def _consume_anthropic_sse(
        cls,
        lines: Any,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        accumulated: List[str] = []
        pending_pre_search: List[str] = []
        current_event: Optional[str] = None
        saw_server_tool_use = False
        saw_search_results = False

        def append_text(text: str) -> None:
            nonlocal pending_pre_search
            if not text:
                return
            if on_chunk is not None:
                accumulated.append(text)
                on_chunk(text)
                return
            if saw_server_tool_use:
                if saw_search_results:
                    accumulated.append(text)
                return
            pending_pre_search.append(text)

        for raw_line in lines:
            if raw_line is None:
                continue
            line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="ignore")
            if not line:
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                current_event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue

            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except Exception:
                continue

            event_type = str(data.get("type") or current_event or "").strip()
            if event_type == "content_block_start":
                block = data.get("content_block") or {}
                block_type = block.get("type")
                if block_type == "server_tool_use":
                    saw_server_tool_use = True
                    if on_chunk is None:
                        pending_pre_search = []
                elif block_type == "web_search_tool_result":
                    saw_search_results = True
                elif block_type == "text":
                    append_text(str(block.get("text") or ""))
            elif event_type == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta":
                    append_text(str(delta.get("text") or ""))
            elif event_type == "message_stop":
                break

            current_event = None

        if on_chunk is None and not saw_server_tool_use and pending_pre_search:
            accumulated = pending_pre_search + accumulated

        if not accumulated:
            return None
        return cls._clean_anthropic_text("".join(accumulated))

    @staticmethod
    def _clean_anthropic_text(text: str) -> str:
        cleaned = (text or "").strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = re.sub(r"(?is)<function_calls>.*?</function_calls>", "", cleaned).strip()
        cleaned = re.sub(r"(?is)<invoke\b.*?</invoke>", "", cleaned).strip()
        cleaned = re.sub(r"(?is)<parameter\b[^>]*>.*?</parameter>", "", cleaned).strip()
        return cleaned.strip()

    @staticmethod
    def _extract_references(markdown_text: str, links_block: str = "") -> List[Dict[str, str]]:
        references: List[Dict[str, str]] = []
        seen_urls: set = set()

        def add_reference(title: str, url: str) -> None:
            clean_url = (url or "").strip()
            if not re.match(r"^https?://", clean_url, flags=re.IGNORECASE):
                return
            key = clean_url.lower()
            if key in seen_urls:
                return
            seen_urls.add(key)
            clean_title = (title or "").strip().strip("<>").strip("⟨⟩").strip()
            references.append({
                "title": clean_title or clean_url,
                "url": clean_url,
            })

        if links_block.strip():
            for entry in [part.strip() for part in links_block.split(";") if part.strip()]:
                if "::" in entry:
                    title, url = entry.split("::", 1)
                    add_reference(title, url)
                    continue

                md_match = re.search(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", entry)
                if md_match:
                    add_reference(md_match.group(1), md_match.group(2))
                    continue

                url_match = re.search(r"(https?://\S+)", entry)
                if url_match:
                    url = url_match.group(1).rstrip(";,)")
                    title = entry.replace(url_match.group(1), "").strip(" -:")
                    add_reference(title, url)

        for md_match in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", markdown_text):
            add_reference(md_match.group(1), md_match.group(2))

        return references

    def _format_answer_for_ui(self, raw: str) -> Dict[str, Any]:
        text = self._reformat_citations(raw or "")
        if not text:
            return {"answer": "", "references": []}

        summary_block = extract_delimited_block(text, "SUMMARY")
        latex_block = extract_delimited_block(text, "LATEX")
        links_block = extract_delimited_block(text, "LINKS")
        suggestions_block = extract_delimited_block(text, "SUGGESTIONS")

        extracted_code_blocks: List[str] = []

        def extract_code(match: re.Match) -> str:
            lang = (match.group(1) or "").strip()
            code = (match.group(2) or "").strip("\n")
            if not code:
                return ""
            fence = f"```{lang}\n{code}\n```" if lang else f"```\n{code}\n```"
            extracted_code_blocks.append(fence)
            return ""

        body = re.sub(
            r"CODE_START\[(.*?)\]\((.*?)\)CODE_END",
            extract_code,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        body = self._strip_delimited_block(body, "SUMMARY")
        body = self._strip_delimited_block(body, "LATEX")
        body = self._strip_delimited_block(body, "LINKS")
        body = self._strip_delimited_block(body, "SUGGESTIONS")
        body = body.strip()

        parts: List[str] = []
        if body:
            parts.append(body)
        elif summary_block.strip():
            parts.append(summary_block.strip())

        if latex_block.strip():
            latex_content = latex_block.strip()
            if "$" not in latex_content:
                latex_content = f"$$\n{latex_content}\n$$"
            parts.append(latex_content)

        if extracted_code_blocks:
            parts.extend(extracted_code_blocks)

        suggestions = [s.strip() for s in suggestions_block.split(";") if s.strip()]
        if suggestions:
            suggestions_md = "### Suggested follow-ups\n" + "\n".join(f"- {item}" for item in suggestions[:5])
            parts.append(suggestions_md)

        markdown = "\n\n".join(part for part in parts if part.strip()).strip()
        markdown = re.sub(r"\n{3,}", "\n\n", markdown)
        if not markdown:
            markdown = text

        references = self._extract_references(markdown, links_block)
        return {
            "answer": markdown.strip(),
            "references": references,
        }

    # ---- Transcript window (matches cosmic-prod exactly) ----

    def _transcript_window(
        self,
        duration_seconds: int = 20,
        latest_seconds: int = 8,
        include_segment_ids: bool = False,
    ) -> str:
        with self.lock:
            now_mt = self.current_meeting_time()
            full_cutoff = now_mt - duration_seconds
            latest_cutoff = now_mt - latest_seconds
            older: List[str] = []
            latest: List[str] = []

            for seg in self.transcript:
                mt = float(seg.get("meeting_time", 0.0))
                if mt < full_cutoff:
                    continue
                speaker = seg.get("speaker", "Speaker")
                text = seg.get("text", "")
                segment_id = seg.get("segment_id")
                if include_segment_ids and segment_id is not None:
                    line = f"[#{segment_id}] [{speaker}] {text}"
                else:
                    line = f"[{speaker}] {text}"
                if mt >= latest_cutoff:
                    latest.append(line)
                else:
                    older.append(line)

        parts: List[str] = []
        if older:
            parts.append("\n".join(older))
        if latest:
            parts.append("\n--- LATEST PART OF CONVERSATION (PRIORITIZE THIS) ---")
            parts.append("\n".join(latest))
        return "\n".join(parts)

    # ---- Summary history (matches cosmic-prod exactly) ----

    def _summary_history(self, minutes_back: int = 3, max_items: int = 12) -> str:
        if not self.summaries:
            return ""

        current_mt = self.current_meeting_time()
        cutoff = current_mt - (minutes_back * 60) if minutes_back > 0 else None
        collected: List[str] = []

        for item in reversed(self.summaries):
            if isinstance(item, dict):
                content = str(item.get("content", "")).strip()
                mt = item.get("meeting_time")
            else:
                content = str(item).strip()
                mt = None

            if not content:
                continue
            if cutoff is not None and isinstance(mt, (int, float)) and mt < cutoff:
                continue

            collected.append(content)
            if len(collected) >= max_items:
                break

        chronological = list(reversed(collected))
        compacted = compact_summary_lines(chronological)
        return "\n".join(compacted[-max_items:])

    # ---- Control ----

    def pause(self) -> None:
        if self.is_paused:
            return
        self.is_paused = True
        self.pause_started_at = time.time()
        db.set_meeting_status(self.meeting_id, "paused")
        emit_status("paused", meeting_id=self.meeting_id)

    def resume(self) -> None:
        if not self.is_paused:
            return
        self.is_paused = False
        if self.pause_started_at:
            self.total_pause_duration += max(0.0, time.time() - self.pause_started_at)
        self.pause_started_at = None
        db.set_meeting_status(self.meeting_id, "active")
        emit_status("running", meeting_id=self.meeting_id)

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self.stop_event.clear()
        self.new_transcript_event.clear()

        self.processor_thread = threading.Thread(target=self._process_loop, daemon=True)
        self.processor_thread.start()

        self.audio_thread = threading.Thread(target=self._run_audio_loop, daemon=True)
        self.audio_thread.start()

        emit_status("running", meeting_id=self.meeting_id, title=self.title)

    def stop(self) -> Dict[str, Any]:
        self.is_running = False
        self.stop_event.set()
        self.new_transcript_event.set()

        if self.websocket_conn and self.audio_loop and self.audio_loop.is_running():
            ws = self.websocket_conn
            self.websocket_conn = None
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), self.audio_loop)
            except Exception:
                pass

        if self.processor_thread and self.processor_thread.is_alive():
            self.processor_thread.join(timeout=2.5)
        if self.audio_thread and self.audio_thread.is_alive():
            self.audio_thread.join(timeout=3.0)

        db.end_meeting(self.meeting_id)
        emit_status("stopped", meeting_id=self.meeting_id)
        return self.build_final_report()

    def add_transcript_segment(self, speaker: str, text: str, is_final: bool, confidence: float = 0.0) -> None:
        if not text or not text.strip():
            return
        if self.is_paused:
            return

        raw_text = str(text or "").strip()
        clean_text = self._normalize_transcript_text(raw_text)
        if not clean_text:
            return

        if is_final:
            with self.lock:
                for seg in reversed(self.transcript):
                    if not isinstance(seg, dict) or not seg.get("is_final"):
                        continue
                    last_speaker = str(seg.get("speaker", "")).strip()
                    last_text = str(seg.get("text", "")).strip()
                    last_ts = float(seg.get("timestamp", 0.0) or 0.0)
                    same_signature = self._segment_signature(last_speaker, last_text) == self._segment_signature(speaker, clean_text)
                    close_in_time = (time.time() - last_ts) <= 3.0
                    if same_signature and close_in_time:
                        return
                    break

        segment_id: Optional[int] = None
        timestamp = time.time()
        if is_final:
            segment_id = db.add_meeting_transcript(
                self.meeting_id,
                speaker=speaker,
                text=clean_text,
                is_final=True,
                confidence=confidence,
                timestamp=timestamp,
                raw_text=raw_text,
            )

        payload = {
            "segment_id": segment_id,
            "speaker": speaker,
            "text": clean_text,
            "raw_text": raw_text,
            "is_final": bool(is_final),
            "confidence": float(confidence or 0.0),
            "meeting_time": self.current_meeting_time(),
            "timestamp": timestamp,
        }
        with self.lock:
            self.transcript.append(payload)

        emit("MEETING_TRANSCRIPT", payload)

        if is_final:
            self.pending_interim = None
            self.new_transcript_event.set()

    # ---- Process loop ----

    def _process_loop(self) -> None:
        while not self.stop_event.is_set():
            signaled = self.new_transcript_event.wait(timeout=0.5)
            if self.stop_event.is_set() or not self.is_running:
                break
            if signaled:
                self.new_transcript_event.clear()
                self._pending_generation = True

            if not self._pending_generation:
                continue

            elapsed_since_last = time.time() - self.last_update_time
            if elapsed_since_last < self.update_interval_sec:
                continue

            self._pending_generation = False
            self._generate_update()

    # ---- Orchestrator core ----

    def _has_meaningful_content(self, text: str) -> bool:
        """Skip LLM only when the transcript window is completely empty (no speech at all)."""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        content_lines = [ln for ln in lines if not ln.startswith("---")]
        if not content_lines:
            return False
        total_words = sum(len(ln.split()) for ln in content_lines)
        return total_words >= 1

    def _generate_update(self) -> None:
        if self.is_paused:
            return

        transcript_window = self._transcript_window(30, include_segment_ids=True)
        if not transcript_window.strip():
            return

        if not self._has_meaningful_content(transcript_window):
            return

        current_hash = hashlib.md5(transcript_window.encode("utf-8")).hexdigest()
        if current_hash == self.last_hash:
            return
        self.last_hash = current_hash
        self.last_update_time = time.time()

        summary_ctx = self._summary_history(minutes_back=4, max_items=15)
        update = self._call_summary_llm(transcript_window, summary_ctx)
        if not update:
            return

        self._apply_transcript_fixes(update.transcript_fixes)
        if update.transcript_fixes:
            transcript_window = self._transcript_window(30, include_segment_ids=True)

        if not update.nudge and self._needs_nudge_repair(transcript_window, update.cues):
            repaired_nudge = self._call_nudge_repair_llm(
                transcript_window=transcript_window,
                summary_history=summary_ctx,
                summary=update.summary,
                cues=update.cues,
            )
            if repaired_nudge:
                update.nudge = repaired_nudge

        norm_new = normalize_summary(update.summary)
        norm_old = normalize_summary(self.last_summary)
        summary_is_duplicate = False
        if norm_new and norm_new == norm_old:
            summary_is_duplicate = True
        elif self.last_summary and summary_similarity(self.last_summary, update.summary) >= 0.86:
            if len(norm_new) <= int(len(norm_old) * 1.05):
                summary_is_duplicate = True

        nudge_to_emit = update.nudge
        if (
            nudge_to_emit
            and normalize_summary(nudge_to_emit) == normalize_summary(self.last_nudge)
            and (time.time() - self.last_nudge_time) < NUDGE_REPEAT_COOLDOWN_SEC
        ):
            nudge_to_emit = ""

        # Important: allow nudge/cues/action-only updates even when summary doesn't change.
        has_non_summary_signal = bool(nudge_to_emit or update.cues or update.action_items)
        if summary_is_duplicate and not has_non_summary_signal:
            return

        mt_now = self.current_meeting_time()
        summary_to_store = update.summary
        if summary_is_duplicate:
            summary_to_store = ""
        else:
            self.last_summary = update.summary
            self.summaries.append({
                "content": update.summary,
                "meeting_time": mt_now,
                "created_at": time.time(),
            })

        self.action_items.extend(update.action_items)
        self.action_items = unique_ordered(self.action_items)

        if nudge_to_emit:
            self.last_nudge = nudge_to_emit
            self.last_nudge_time = time.time()

        db.add_meeting_update(
            self.meeting_id,
            summary=summary_to_store,
            cues=update.cues,
            nudge=nudge_to_emit,
            action_items=update.action_items,
        )
        emit("MEETING_UPDATE", {
            "summary": summary_to_store,
            "cues": update.cues,
            "nudge": nudge_to_emit,
            "action_items": update.action_items,
            "meeting_time": mt_now,
            "timestamp": time.time(),
        })

    def _call_summary_llm(self, transcript_window: str, summary_history: str) -> Optional[MeetingUpdate]:
        if not self.anthropic_key:
            return None

        instructions_block = self._build_instructions_block()
        nudge_behavior_block = self._build_nudge_behavior_block()
        system_prompt = STRICT_TIER_A_PROMPT.format(
            user_name=self.user_name,
            custom_instructions_block=instructions_block,
            nudge_behavior_block=nudge_behavior_block,
            live_web_rules_block=self._build_live_web_rules_block(),
        )

        user_content = ""
        if summary_history.strip():
            user_content += f"<summary_history>\n{summary_history}\n</summary_history>\n\n"
        if self.last_nudge.strip():
            user_content += f"<previous_nudge>\n{self.last_nudge.strip()}\n</previous_nudge>\n\n"
        user_content += f"<transcript>\n{transcript_window}\n</transcript>"

        raw = self._call_anthropic(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.2,
            max_tokens=768,
            allow_web=self.web_search_enabled,
        )
        if not raw:
            return None
        return self._parse_structured_update(raw)

        # --- Groq (retained for fallback) ---
        # messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
        # raw = self._call_groq(messages, temperature=0.3, allow_web=True)
        # if not raw: return None
        # return self._parse_structured_update(raw)

    def _call_nudge_repair_llm(
        self,
        transcript_window: str,
        summary_history: str,
        summary: str,
        cues: List[str],
    ) -> str:
        if not self.anthropic_key:
            return ""

        instructions_block = self._build_instructions_block()
        system_prompt = NUDGE_REPAIR_PROMPT.format(
            user_name=self.user_name,
            custom_instructions_block=instructions_block,
            live_web_rules_block=self._build_live_web_rules_block(),
        )

        user_parts = [f"<meeting_profile>\n{self._build_meeting_profile_block()}\n</meeting_profile>"]
        if summary_history.strip():
            user_parts.append(f"<summary_history>\n{summary_history}\n</summary_history>")
        if self.last_nudge.strip():
            user_parts.append(f"<previous_nudge>\n{self.last_nudge.strip()}\n</previous_nudge>")
        if summary.strip():
            user_parts.append(f"<latest_summary>\n{summary.strip()}\n</latest_summary>")
        if cues:
            user_parts.append("<candidate_cues>\n" + "\n".join(f"- {cue}" for cue in cues[:3]) + "\n</candidate_cues>")
        user_parts.append(f"<transcript>\n{transcript_window}\n</transcript>")

        raw = self._call_anthropic(
            system_prompt=system_prompt,
            user_content="\n\n".join(user_parts),
            temperature=0.2,
            max_tokens=220,
            allow_web=self.web_search_enabled,
        )
        return self._normalize_transcript_text(raw or "")

    # ---- Ask / Cue click (matches cosmic-prod _process_cue_click_async) ----

    def ask(self, question: str, allow_web_search: bool = False) -> Dict[str, Any]:
        question = (question or "").strip()
        if not question:
            return {"answer": "Please provide a question.", "references": []}

        if self.is_running and self.is_paused:
            return {"answer": "Meeting is paused. Resume it to ask a live question.", "references": []}

        answer_context = self._build_answer_context()
        summary_text = answer_context["summary_text"]
        transcript_text = answer_context["transcript_text"]

        if not self.anthropic_key:
            return self._fallback_answer(question, transcript_text, summary_text)

        instructions_block = self._build_instructions_block()
        prompt_template = STRICT_EXPLAINER_PROMPT if allow_web_search else STRICT_EXPLAINER_PROMPT_NO_WEB
        system_prompt = prompt_template.format(
            user_name=self.user_name,
            custom_instructions_block=instructions_block,
        )

        user_content = ""
        if allow_web_search:
            user_content += "Web search is ON. Use it for anything that implies latest, current, recent, or external info.\n\n"
        user_content += f"<meeting_profile>\n{self._build_meeting_profile_block()}\n</meeting_profile>\n\n"
        user_content += f"Address the reader as \"you\". The reader is {self.user_name}; do not refer to them by name in third person.\n\n"
        user_content += f"<summary_history>\n{summary_text}\n</summary_history>\n\n"
        if transcript_text.strip():
            user_content += f"<recent_transcript>\n{transcript_text}\n</recent_transcript>\n\n"
        user_content += f"<question>\n{question}\n</question>"

        answer = self._call_anthropic(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.45,
            max_tokens=1024,
            allow_web=bool(allow_web_search),
        )
        if answer is None:
            return self._fallback_answer(question, transcript_text, summary_text)
        if not answer.strip():
            return self._fallback_answer(question, transcript_text, summary_text)

        return self._format_answer_for_ui(answer)

    def ask_stream(
        self,
        question: str,
        allow_web_search: bool = False,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Same as ask() but streams text via on_chunk(question, chunk) before returning final payload."""
        question = (question or "").strip()
        if not question:
            return {"answer": "Please provide a question.", "references": []}

        if self.is_running and self.is_paused:
            return {"answer": "Meeting is paused. Resume it to ask a live question.", "references": []}

        answer_context = self._build_answer_context()
        summary_text = answer_context["summary_text"]
        transcript_text = answer_context["transcript_text"]

        if not self.anthropic_key:
            return self._fallback_answer(question, transcript_text, summary_text)

        instructions_block = self._build_instructions_block()
        prompt_template = STRICT_EXPLAINER_PROMPT if allow_web_search else STRICT_EXPLAINER_PROMPT_NO_WEB
        system_prompt = prompt_template.format(
            user_name=self.user_name,
            custom_instructions_block=instructions_block,
        )
        user_content = ""
        if allow_web_search:
            user_content += "Web search is ON. Use it for anything that implies latest, current, recent, or external info.\n\n"
        user_content += f"<meeting_profile>\n{self._build_meeting_profile_block()}\n</meeting_profile>\n\n"
        user_content += f"Address the reader as \"you\". The reader is {self.user_name}; do not refer to them by name in third person.\n\n"
        user_content += f"<summary_history>\n{summary_text}\n</summary_history>\n\n"
        if transcript_text.strip():
            user_content += f"<recent_transcript>\n{transcript_text}\n</recent_transcript>\n\n"
        user_content += f"<question>\n{question}\n</question>"

        answer = self._call_anthropic_stream(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=0.45,
            max_tokens=1024,
            allow_web=bool(allow_web_search),
            on_chunk=on_chunk,
        )
        if answer is None:
            return self._fallback_answer(question, transcript_text, summary_text)
        if not answer.strip():
            return self._fallback_answer(question, transcript_text, summary_text)
        return self._format_answer_for_ui(answer)

    def _fallback_answer(self, question: str, transcript_text: str, updates_text: str) -> Dict[str, Any]:
        context_text = (transcript_text or "").strip()
        if (not context_text or context_text.lower().startswith("transcript omitted intentionally")) and updates_text.strip():
            context_text = updates_text.strip()

        if not context_text:
            return {
                "answer": f"I couldn't get a response right now. Try asking again: **{question}**",
                "references": [],
            }
        return {
            "answer": (
                f"The AI couldn't generate a full answer right now. "
                f"Here's what I know from the meeting so far:\n\n"
                + "\n".join(f"- {ln}" for ln in context_text.splitlines()[-8:] if ln.strip())
                + f"\n\nTry asking again: **{question}**"
            ),
            "references": [],
        }

    # ---- Anthropic (Claude Haiku 4.5) transport ----

    def _call_anthropic_http2_stream(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float,
        max_tokens: int,
        allow_web: bool,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        client = _get_anthropic_http2_client()
        if client is None:
            return None

        payload = self._build_anthropic_payload(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=max_tokens,
            allow_web=allow_web,
            stream=True,
        )
        headers = self._anthropic_headers(self.anthropic_key)

        try:
            with client.stream(
                "POST",
                f"{ANTHROPIC_BASE}/messages",
                headers=headers,
                json=payload,
                timeout=self._anthropic_timeout(stream=True),
            ) as resp:
                if resp.status_code != 200:
                    text = ""
                    try:
                        text = resp.text[:300]
                    except Exception:
                        pass
                    dlog("Anthropic HTTP/2 stream error:", resp.status_code, text)
                    if allow_web and resp.status_code in (400, 401, 403):
                        return self._call_anthropic_http2_stream(
                            system_prompt=system_prompt,
                            user_content=user_content,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            allow_web=False,
                            on_chunk=on_chunk,
                        )
                    return None
                return self._consume_anthropic_sse(resp.iter_lines(), on_chunk=on_chunk)
        except Exception as exc:
            dlog("Anthropic HTTP/2 stream failed:", exc)
            return None

    def _call_anthropic_requests(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float,
        max_tokens: int,
        allow_web: bool,
    ) -> Optional[str]:
        payload = self._build_anthropic_payload(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=max_tokens,
            allow_web=allow_web,
            stream=False,
        )
        try:
            resp = requests.post(
                f"{ANTHROPIC_BASE}/messages",
                headers=self._anthropic_headers(self.anthropic_key),
                data=json.dumps(payload),
                timeout=self._anthropic_timeout(stream=False),
            )
            if resp.status_code != 200:
                dlog("Anthropic requests fallback error:", resp.status_code, resp.text[:300])
                if allow_web and resp.status_code in (400, 401, 403):
                    return self._call_anthropic_requests(
                        system_prompt=system_prompt,
                        user_content=user_content,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        allow_web=False,
                    )
                return None
            body = resp.json()
            return self._extract_anthropic_text_from_blocks(body.get("content") or [])
        except Exception as exc:
            dlog("Anthropic requests fallback failed:", exc)
            return None

    def _call_anthropic_stream_requests(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float,
        max_tokens: int,
        allow_web: bool,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        payload = self._build_anthropic_payload(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=max_tokens,
            allow_web=allow_web,
            stream=True,
        )
        try:
            resp = requests.post(
                f"{ANTHROPIC_BASE}/messages",
                headers=self._anthropic_headers(self.anthropic_key),
                data=json.dumps(payload),
                timeout=self._anthropic_timeout(stream=True),
                stream=True,
            )
            if resp.status_code != 200:
                preview = ""
                try:
                    preview = resp.text[:300]
                except Exception:
                    pass
                dlog("Anthropic requests stream fallback error:", resp.status_code, preview)
                if allow_web and resp.status_code in (400, 401, 403):
                    return self._call_anthropic_stream_requests(
                        system_prompt=system_prompt,
                        user_content=user_content,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        allow_web=False,
                        on_chunk=on_chunk,
                    )
                return None
            return self._consume_anthropic_sse(resp.iter_lines(decode_unicode=True), on_chunk=on_chunk)
        except Exception as exc:
            dlog("Anthropic requests stream fallback failed:", exc)
            return None

    def _call_anthropic(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.4,
        max_tokens: int = 1024,
        allow_web: bool = False,
    ) -> Optional[str]:
        if not self.anthropic_key:
            return None

        # Use streaming transport for all Anthropic calls so we can keep a warm HTTP/2 connection.
        response = self._call_anthropic_http2_stream(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=max_tokens,
            allow_web=allow_web,
            on_chunk=None,
        )
        if response is not None:
            return response

        return self._call_anthropic_requests(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=max_tokens,
            allow_web=allow_web,
        )

    def _call_anthropic_stream(
        self,
        system_prompt: str,
        user_content: str,
        temperature: float = 0.4,
        max_tokens: int = 1024,
        allow_web: bool = False,
        on_chunk: Optional[Callable[[str], None]] = None,
    ) -> Optional[str]:
        """Stream Anthropic response; call on_chunk(text) for each text delta. Returns final merged text or None."""
        if not self.anthropic_key:
            return None

        streamed_parts: List[str] = []

        def tracked_on_chunk(text: str) -> None:
            if not text:
                return
            streamed_parts.append(text)
            if on_chunk:
                on_chunk(text)

        response = self._call_anthropic_http2_stream(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=max_tokens,
            allow_web=allow_web,
            on_chunk=tracked_on_chunk,
        )
        if response is not None:
            return response
        if streamed_parts:
            return self._clean_anthropic_text("".join(streamed_parts))

        fallback_response = self._call_anthropic_stream_requests(
            system_prompt=system_prompt,
            user_content=user_content,
            temperature=temperature,
            max_tokens=max_tokens,
            allow_web=allow_web,
            on_chunk=tracked_on_chunk,
        )
        if fallback_response is not None:
            return fallback_response
        if streamed_parts:
            return self._clean_anthropic_text("".join(streamed_parts))
        return None

    # ---- Groq HTTP call (commented out — retained for fallback) ----
    # def _call_groq(
    #     self,
    #     messages: List[Dict[str, Any]],
    #     temperature: float = 0.4,
    #     allow_web: bool = False,
    #     max_tokens: Optional[int] = None,
    # ) -> Optional[str]:
    #     if not self.groq_key:
    #         return None
    #     payload: Dict[str, Any] = {
    #         "model": GROQ_MODEL,
    #         "messages": messages,
    #         "temperature": temperature,
    #         "stream": False,
    #     }
    #     if isinstance(max_tokens, int) and max_tokens > 0:
    #         payload["max_tokens"] = max_tokens
    #     if allow_web:
    #         payload["tools"] = [{"type": "browser_search"}]
    #     try:
    #         resp = requests.post(
    #             "https://api.groq.com/openai/v1/chat/completions",
    #             headers={
    #                 "Authorization": f"Bearer {self.groq_key}",
    #                 "Content-Type": "application/json",
    #             },
    #             data=json.dumps(payload),
    #             timeout=30,
    #         )
    #         if resp.status_code != 200:
    #             dlog("Groq error:", resp.status_code, resp.text[:300])
    #             return None
    #         body = resp.json()
    #         choices = body.get("choices", [])
    #         if not choices:
    #             return None
    #         content_parts: List[str] = []
    #         for choice in choices:
    #             msg = choice.get("message", {})
    #             c = (msg.get("content") or "").strip()
    #             if c:
    #                 content_parts.append(c)
    #         if content_parts:
    #             return "\n\n".join(content_parts)
    #         first_msg = choices[0].get("message", {})
    #         content = (first_msg.get("content") or "").strip()
    #         return content if content else None
    #     except Exception as exc:
    #         dlog("Groq request failed:", exc)
    #         return None

    # ---- Final report ----

    def build_final_report(self) -> Dict[str, Any]:
        report = db.get_meeting_report(self.meeting_id)
        updates = report.get("context", {}).get("updates", [])
        summaries = unique_ordered([u.get("summary", "") for u in updates if u.get("summary")])

        action_items_db = [row.get("action_item", "") for row in report.get("action_items", [])]
        merged_actions = unique_ordered(self.action_items + action_items_db)

        final_summary = " ".join(summaries[-8:]).strip()
        if not final_summary:
            final_summary = "Meeting completed. No structured summary was generated."

        payload = {
            "meeting_id": self.meeting_id,
            "title": self.title,
            "summary": final_summary,
            "action_items": merged_actions,
            "timestamp": time.time(),
        }
        emit("MEETING_FINAL", payload)
        return payload

    # ---- Speaker identification (Nova v1 response format) ----

    def _identify_speaker(self, data: Dict[str, Any]) -> str:
        """Extract speaker from Nova v1 diarization data."""
        channel = data.get("channel", {})
        alts = channel.get("alternatives", [])
        if alts:
            words = alts[0].get("words", [])
            if words and isinstance(words[0], dict):
                dg_speaker = words[0].get("speaker")
                if dg_speaker is not None:
                    if int(dg_speaker) == 0:
                        return "Me"
                    return f"Speaker {dg_speaker}"
        return "Me"

    def _filter_audio_frame(self, data: bytes) -> bytes:
        """
        Apply a light adaptive noise gate.
        Higher sensitivity lowers the threshold; lower sensitivity raises it.
        Silence frames are sent as zeroed PCM so Deepgram timing remains stable.
        """
        if not data:
            return data

        try:
            rms = float(audioop.rms(data, 2))
        except Exception:
            return data

        if self.calibration_frames < 24:
            self.noise_floor_rms = (
                rms if self.calibration_frames == 0
                else (self.noise_floor_rms * 0.72) + (rms * 0.28)
            )
            self.calibration_frames += 1
            return data

        if rms <= max(18.0, self.noise_floor_rms * 1.18):
            self.noise_floor_rms = (self.noise_floor_rms * 0.94) + (rms * 0.06)
        else:
            self.noise_floor_rms = (
                self.noise_floor_rms * 0.995
            ) + (min(rms, self.noise_floor_rms * 2.4) * 0.005)

        sensitivity = self.mic_sensitivity / 100.0
        gate_multiplier = 1.68 - (0.9 * sensitivity)
        minimum_gate = 78.0 - (34.0 * sensitivity)
        threshold = max(minimum_gate, self.noise_floor_rms * gate_multiplier)

        if rms >= threshold:
            self.speech_hold_frames = 3
            return data

        if self.speech_hold_frames > 0:
            self.speech_hold_frames -= 1
            return data

        return b"\x00" * len(data)

    # ---- Audio capture (Deepgram Nova-3 via /v1/listen) ----

    def _run_audio_loop(self) -> None:
        self.audio_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.audio_loop)
        try:
            self.audio_loop.run_until_complete(self._audio_capture_task())
        except Exception as exc:
            emit_status("error", meeting_id=self.meeting_id, error=f"Audio loop error: {exc}")
        finally:
            try:
                pending = asyncio.all_tasks(self.audio_loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.audio_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                self.audio_loop.run_until_complete(self.audio_loop.shutdown_asyncgens())
            except Exception:
                pass
            self.audio_loop.close()
            self.audio_loop = None

    async def _audio_capture_task(self) -> None:
        if not HAS_PYAUDIO:
            emit_status("error", meeting_id=self.meeting_id, error="PyAudio not installed")
            return
        if not self.deepgram_key:
            emit_status("error", meeting_id=self.meeting_id, error="Deepgram API key missing")
            return

        p = pyaudio.PyAudio()
        audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)

        try:
            self.audio_stream = p.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=TARGET_SR,
                input=True,
                frames_per_buffer=SAMPLES_PER_FRAME,
            )

            async def capture() -> None:
                ev_loop = asyncio.get_running_loop()
                while self.is_running and not self.stop_event.is_set():
                    if self.is_paused:
                        await asyncio.sleep(0.05)
                        continue
                    try:
                        data = await ev_loop.run_in_executor(
                            None,
                            lambda: self.audio_stream.read(SAMPLES_PER_FRAME, exception_on_overflow=False),
                        )
                        if data:
                            data = self._filter_audio_frame(data)
                            try:
                                audio_queue.put_nowait(data)
                            except asyncio.QueueFull:
                                try:
                                    audio_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                                audio_queue.put_nowait(data)
                    except Exception:
                        break

            async def stream_to_deepgram() -> None:
                from urllib.parse import urlencode
                params = {
                    "model": DG_MODEL,
                    "encoding": "linear16",
                    "sample_rate": str(TARGET_SR),
                    "channels": "1",
                    "interim_results": "true",
                    "punctuate": "true",
                    "smart_format": "true",
                    "diarize": "true",
                    "utterances": "true",
                    "timestamps": "true",
                }
                url = f"{DG_WS}?{urlencode(params)}"
                auth_header = [("Authorization", f"Token {self.deepgram_key}")]

                for attempt in range(DG_MAX_RETRIES):
                    if not self.is_running or self.stop_event.is_set():
                        return
                    try:
                        ws = await asyncio.wait_for(
                            websockets.connect(
                                url,
                                extra_headers=auth_header,
                                ping_interval=DG_WS_PING_INTERVAL,
                                ping_timeout=DG_WS_PING_TIMEOUT,
                                max_size=DG_WS_MAX_SIZE,
                            ),
                            timeout=DG_CONNECT_TIMEOUT,
                        )
                        self.websocket_conn = ws
                        emit_status("listening", meeting_id=self.meeting_id)
                        dlog(f"Deepgram Nova-3 connected (attempt {attempt + 1})")

                        async def sender() -> None:
                            while self.is_running and not self.stop_event.is_set():
                                try:
                                    data = await asyncio.wait_for(audio_queue.get(), timeout=0.5)
                                    await ws.send(data)
                                except asyncio.TimeoutError:
                                    continue
                                except Exception as exc:
                                    dlog("Sender error:", exc)
                                    break

                        async def receiver() -> None:
                            while self.is_running and not self.stop_event.is_set():
                                try:
                                    message = await asyncio.wait_for(ws.recv(), timeout=30.0)
                                    data = json.loads(message)
                                    msg_type = data.get("type", "")

                                    if msg_type == "Metadata":
                                        dlog("Deepgram metadata:", data.get("request_id", ""))
                                        continue
                                    if msg_type == "SpeechStarted":
                                        continue
                                    if msg_type == "UtteranceEnd":
                                        pending = self.pending_interim
                                        if pending:
                                            self.add_transcript_segment(
                                                str(pending.get("speaker") or "Speaker"),
                                                str(pending.get("text") or ""),
                                                True,
                                                float(pending.get("confidence") or 0.0),
                                            )
                                        continue
                                    if msg_type == "Error":
                                        err = data.get("message", data.get("description", "Unknown error"))
                                        dlog("Deepgram error:", err)
                                        emit_status("error", meeting_id=self.meeting_id, error=f"Deepgram: {err}")
                                        break

                                    channel = data.get("channel", {})
                                    alts = channel.get("alternatives", [])
                                    if not alts:
                                        continue
                                    transcript = (alts[0].get("transcript") or "").strip()
                                    if not transcript:
                                        continue

                                    is_final = bool(data.get("is_final", False))
                                    confidence = float(alts[0].get("confidence", 0.0))
                                    speaker = self._identify_speaker(data)

                                    if is_final:
                                        self.add_transcript_segment(speaker, transcript, True, confidence)
                                    else:
                                        self.pending_interim = {
                                            "speaker": speaker,
                                            "text": transcript,
                                            "confidence": confidence,
                                            "timestamp": time.time(),
                                        }

                                except asyncio.TimeoutError:
                                    continue
                                except websockets.exceptions.ConnectionClosed as exc:
                                    dlog(f"Deepgram WebSocket closed: code={exc.code} reason={exc.reason}")
                                    break
                                except Exception as exc:
                                    dlog("Receiver error:", exc)
                                    break

                        async def keepalive() -> None:
                            while self.is_running and not self.stop_event.is_set():
                                try:
                                    await asyncio.sleep(DG_KEEPALIVE_INTERVAL)
                                    await ws.send(json.dumps({"type": "KeepAlive"}))
                                except Exception:
                                    break

                        try:
                            await asyncio.gather(sender(), receiver(), keepalive())
                        finally:
                            self.websocket_conn = None
                            try:
                                await ws.send(json.dumps({"type": "CloseStream"}))
                            except Exception:
                                pass
                            try:
                                await ws.close()
                            except Exception:
                                pass
                        return

                    except (asyncio.TimeoutError, OSError, websockets.exceptions.WebSocketException) as exc:
                        dlog(f"Deepgram connect attempt {attempt + 1}/{DG_MAX_RETRIES} failed:", exc)
                        if attempt < DG_MAX_RETRIES - 1:
                            await asyncio.sleep(2 ** attempt)
                        else:
                            emit_status("error", meeting_id=self.meeting_id,
                                        error=f"Deepgram connection failed after {DG_MAX_RETRIES} attempts")

            await asyncio.gather(capture(), stream_to_deepgram())

        except Exception as exc:
            emit_status("error", meeting_id=self.meeting_id, error=f"Deepgram stream error: {exc}")
        finally:
            self.websocket_conn = None
            stream = self.audio_stream
            self.audio_stream = None
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            try:
                p.terminate()
            except Exception:
                pass


CURRENT_MEETING: Optional[MeetingRuntime] = None


def build_key_status() -> Dict[str, Any]:
    deepgram = bool(get_service_key("deepgram", "DEEPGRAM_API_KEY"))
    anthropic = bool(get_service_key("anthropic", "ANTHROPIC_API_KEY"))
    groq = bool(get_service_key("groq", "GROQ_API_KEY"))
    return {
        "hasKeys": deepgram or anthropic or groq,
        "gemini": False,
        "perplexity": False,
        "deepgram": deepgram,
        "anthropic": anthropic,
        "groq": groq,
    }


async def handle_command(raw: str) -> None:
    global CURRENT_MEETING
    cmd = (raw or "").strip()
    if not cmd:
        return

    if cmd == "CHECK_MEETING_KEYS":
        emit("KEY_STATUS", build_key_status())
        return

    if cmd == "GET_MEETING_SETTINGS":
        emit("MEETING_SETTINGS", get_meeting_settings())
        return

    if cmd.startswith("SAVE_MEETING_SETTINGS:"):
        try:
            payload = json.loads(cmd.split(":", 1)[1])
            if not isinstance(payload, dict):
                raise ValueError("Settings payload must be an object")
        except Exception:
            emit_status("error", error="Invalid SAVE_MEETING_SETTINGS payload")
            return

        settings = save_meeting_settings(payload)
        if CURRENT_MEETING:
            CURRENT_MEETING.apply_settings(settings)
        emit("MEETING_SETTINGS", settings)
        return

    if cmd.startswith("START_MEETING:"):
        try:
            payload = json.loads(cmd.split(":", 1)[1])
        except Exception:
            emit_status("error", error="Invalid START_MEETING payload")
            return

        if CURRENT_MEETING and CURRENT_MEETING.is_running:
            final_payload = CURRENT_MEETING.stop()
            emit("MEETING_FINAL", final_payload)
            CURRENT_MEETING = None

        title = str(payload.get("title") or "Meeting").strip() or "Meeting"
        goal = str(payload.get("goal") or "").strip()
        base_settings = get_meeting_settings()
        user_name = str(payload.get("user_name") or base_settings["name_on_call"]).strip() or DEFAULT_MEETING_SETTINGS["name_on_call"]
        custom_instructions = str(payload.get("custom_instructions") or "").strip()
        web_search_enabled = bool(payload.get("web_search_enabled", False))
        mic_sensitivity = clamp_int(
            payload.get("mic_sensitivity", base_settings["mic_sensitivity"]),
            0,
            100,
        )
        update_interval_sec = clamp_float(
            payload.get("update_interval_sec", base_settings["update_interval_sec"]),
            UPDATE_INTERVAL_MIN,
            UPDATE_INTERVAL_MAX,
        )
        saved_settings = save_meeting_settings({
            "name_on_call": user_name,
            "mic_sensitivity": mic_sensitivity,
            "update_interval_sec": update_interval_sec,
        })

        meeting_id = db.create_meeting(title=title, goal=goal, user_name=user_name)
        CURRENT_MEETING = MeetingRuntime(
            meeting_id=meeting_id,
            title=title,
            user_name=user_name,
            goal=goal,
            custom_instructions=custom_instructions,
            mic_sensitivity=saved_settings["mic_sensitivity"],
            update_interval_sec=saved_settings["update_interval_sec"],
            web_search_enabled=web_search_enabled,
        )
        CURRENT_MEETING.start()
        emit("MEETING_SETTINGS", saved_settings)
        return

    if cmd == "STOP_MEETING":
        if CURRENT_MEETING:
            final_payload = CURRENT_MEETING.stop()
            emit("MEETING_FINAL", final_payload)
            CURRENT_MEETING = None
        return

    if cmd == "PAUSE_MEETING":
        if CURRENT_MEETING:
            CURRENT_MEETING.pause()
        return

    if cmd == "RESUME_MEETING":
        if CURRENT_MEETING:
            CURRENT_MEETING.resume()
        return

    if cmd.startswith("SET_MEETING_WEB_SEARCH:"):
        if not CURRENT_MEETING:
            return
        try:
            payload = json.loads(cmd.split(":", 1)[1])
            CURRENT_MEETING.set_web_search_enabled(payload.get("web_search_enabled", False))
        except Exception:
            emit_status("error", meeting_id=CURRENT_MEETING.meeting_id, error="Invalid SET_MEETING_WEB_SEARCH payload")
        return

    if cmd.startswith("ASK_MEETING:"):
        if not CURRENT_MEETING:
            emit(
                "MEETING_ANSWER",
                {
                    "question": "",
                    "answer": "No active meeting. Start a meeting first.",
                    "references": [],
                    "timestamp": time.time(),
                },
            )
            return
        try:
            payload = json.loads(cmd.split(":", 1)[1])
            question = str(payload.get("question") or "").strip()
            allow_web = bool(payload.get("web_search_enabled", False))
        except Exception:
            emit_status("error", meeting_id=CURRENT_MEETING.meeting_id, error="Invalid ASK_MEETING payload")
            return

        if not question:
            emit(
                "MEETING_ANSWER",
                {
                    "question": "",
                    "answer": "Please enter a question.",
                    "references": [],
                    "timestamp": time.time(),
                },
            )
            return

        def do_ask_stream() -> Dict[str, Any]:
            def on_chunk(chunk: str) -> None:
                emit("MEETING_ANSWER_CHUNK", {"question": question, "chunk": chunk, "timestamp": time.time()})
            return CURRENT_MEETING.ask_stream(question, allow_web, on_chunk=on_chunk)

        loop = asyncio.get_running_loop()
        answer_payload = await loop.run_in_executor(None, do_ask_stream)
        answer_text = ""
        references: List[Dict[str, str]] = []
        if isinstance(answer_payload, dict):
            answer_text = str(answer_payload.get("answer") or "").strip()
            raw_refs = answer_payload.get("references")
            if isinstance(raw_refs, list):
                for item in raw_refs:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    url = str(item.get("url") or "").strip()
                    if not url:
                        continue
                    references.append({
                        "title": title or url,
                        "url": url,
                    })
        else:
            answer_text = str(answer_payload or "").strip()

        emit(
            "MEETING_ANSWER",
            {
                "question": question,
                "answer": answer_text,
                "references": references,
                "timestamp": time.time(),
            },
        )
        return


def input_listener(loop: asyncio.AbstractEventLoop) -> None:
    for line in sys.stdin:
        try:
            asyncio.run_coroutine_threadsafe(handle_command(line.strip()), loop)
        except Exception:
            continue


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    emit_status("ready")
    emit("KEY_STATUS", build_key_status())
    emit("MEETING_SETTINGS", get_meeting_settings())

    bridge_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bridge_loop)
    threading.Thread(target=input_listener, args=(bridge_loop,), daemon=True).start()
    bridge_loop.run_forever()
