"""Budgeting text for an agent's own append-only scratchpad.

A scratchpad like `heartbeat_notes.md` has two regions that behave nothing
alike. The top holds standing state - what is resolved, what is being watched.
The bottom holds an append-only log, so the newest entries are the ones that
say what just happened. Truncating such a file from either end alone destroys
the half that matters.

Production failure this exists to prevent: the heartbeat's ambient view of its
own notes was produced by a generic head-truncating excerpt. The file grew past
the budget, so every beat read the *oldest* 40% - a watchpoint asserting a
move-out deadline the user had already cancelled - and each new note correcting
it landed in the invisible tail. The loop could not be broken from inside,
because the notes it wrote were the notes it could not see.

Two rules follow, and both are enforced here rather than at each call site:

- Keep the head *and* the tail, and say out loud that the middle was dropped.
  Silent truncation is what let a wrong belief look like the whole picture.
- Never split a line. These files are Markdown; half a bullet is worse than no
  bullet, and collapsing newlines turns structure into a run-on blob.
"""

from __future__ import annotations

import re

DEFAULT_HEAD_RATIO = 0.4
ELISION_TEMPLATE = "\n\n[... {dropped} characters of older notes elided; head and most recent entries shown ...]\n\n"

# Beat-log lines are self-observation ("I suppressed", "I delivered"). They are
# never wrong and nothing external can correct them, so they carry no world
# facts worth reconciling - and they would drown the query if included.
_LOG_LINE_MARKERS = ("suppressed", "delivered", "delivering", "no material change")


def excerpt_head_and_tail(
    text: str,
    *,
    limit: int,
    head_ratio: float = DEFAULT_HEAD_RATIO,
) -> str:
    """Fit `text` into `limit` characters, keeping both ends, whole lines only.

    Returns `text` unchanged when it already fits. Otherwise the standing state
    at the top and the most recent entries at the bottom both survive, with an
    explicit marker where the middle was removed.
    """
    if limit <= 0:
        return ""
    source = text or ""
    if len(source) <= limit:
        return source

    marker = ELISION_TEMPLATE.format(dropped=0)
    # Budget for content only; the marker itself has to fit inside `limit`.
    content_budget = limit - len(marker)
    if content_budget <= 0:
        # Degenerate budget: a plain tail beats returning nothing, since the
        # newest entries are the ones a caller most likely needs.
        return source[-limit:]

    head_budget = max(0, int(content_budget * head_ratio))
    tail_budget = content_budget - head_budget

    lines = source.split("\n")
    head_lines = _take_lines(lines, budget=head_budget, from_end=False)
    remaining = lines[len(head_lines):]
    tail_lines = _take_lines(remaining, budget=tail_budget, from_end=True)

    head = "\n".join(head_lines).rstrip()
    tail = "\n".join(tail_lines).lstrip()
    if not head and not tail:
        return source[-limit:]

    dropped = len(source) - len(head) - len(tail)
    marker = ELISION_TEMPLATE.format(dropped=max(0, dropped))
    return f"{head}{marker}{tail}".strip()


def _take_lines(lines: list[str], *, budget: int, from_end: bool) -> list[str]:
    """Greedily take whole lines from one end without exceeding `budget`."""
    if budget <= 0:
        return []
    ordered = reversed(lines) if from_end else iter(lines)
    taken: list[str] = []
    used = 0
    for line in ordered:
        cost = len(line) + 1  # the newline this line will be joined with
        if used + cost > budget:
            break
        taken.append(line)
        used += cost
    if from_end:
        taken.reverse()
    return taken


def truncate_keeping_newest(text: str, *, limit: int) -> str:
    """Cap a growing scratchpad document without discarding what was just added.

    The obvious `text[:limit]` is exactly wrong here: on an append-only file it
    silently drops the newest content, so past the cap the document becomes
    append-proof with no error anywhere. Keep both ends instead, so standing
    state survives and new entries still land.
    """
    return excerpt_head_and_tail(text, limit=limit)


def derive_reconciliation_query(notes_text: str, *, limit: int = 900) -> str:
    """Build a memory-retrieval query out of the claims a scratchpad asserts.

    The point is to retrieve what the agent is *actually about to reason over*,
    so a durable correction to any of it can reach the turn that would
    otherwise act on the stale copy. A fixed generic query cannot do that: on
    live data it did not surface the correcting memory at all, while this one
    ranked it first.

    Beat-log lines are dropped - they are self-observation, not world facts.
    """
    claims: list[str] = []
    for raw in (notes_text or "").split("\n"):
        line = raw.strip().lstrip("#-*").strip()
        if len(line) < 8:
            continue
        lowered = line.lower()
        if any(marker in lowered for marker in _LOG_LINE_MARKERS):
            continue
        if line.startswith("["):  # the elision marker
            continue
        claims.append(line)

    query = " ".join(claims)
    query = re.sub(r"\s+", " ", query).strip()
    if len(query) <= limit:
        return query
    # Cut on a word boundary so the tail of the query is not a fragment.
    clipped = query[:limit]
    space = clipped.rfind(" ")
    return (clipped[:space] if space > limit // 2 else clipped).strip()
