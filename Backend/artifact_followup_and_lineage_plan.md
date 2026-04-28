# Artifact Follow-Up Editing And Lineage Plan

## Starting Incident

This plan starts from a concrete failure observed in a live COSMIC conversation:

1. The user uploaded a `.docx` invoice.
2. COSMIC edited it successfully and returned an updated `.docx`.
3. In the next turn, the user asked for a small follow-up change: update the certifier name and title.
4. Instead of reopening the real edited document and applying a delta edit, COSMIC:
   - lost access to the binary `.docx`
   - loaded session history
   - ran artifact lookup and docs bundle reads
   - attempted unrelated fallback behavior
   - reconstructed the file from parsed text + remembered edits

This is the wrong behavior.

The task itself is simple. The failure initially looked architectural, but the exact immediate bug was narrower:

- `artifact_lookup` returned artifact entries under `results`
- the follow-up staging pipeline only checked `artifacts`
- so the lookup succeeded conceptually, but the file was never staged into the Anthropic code execution sandbox on the next turn
- code execution then could not see the real edited `.docx`

That immediate bug has already been fixed in `orchestrator/runtime.py` by allowing staging extraction to fall back to `results` when `artifacts` is absent.

The broader architecture problem remains:

- turn-1 uploaded artifact editing works
- turn-2 follow-up editing of the produced artifact is unreliable

So this document should be read in two layers:

1. the exact invoice follow-up bug is now understood and fixed
2. the larger artifact continuity and lineage model is still needed

## Problem Summary

COSMIC currently has several disconnected capabilities:

- upload and ingest input artifacts
- produce output artifacts
- parse documents and structured files
- run specialist agents that can create or edit files

What is still missing is a stable artifact continuity model across turns and across sessions.

The result is that follow-up edit requests can degrade into:

- session-history traversal instead of artifact reopening
- parse/read fallbacks instead of real binary edits
- reconstruction from text/history instead of modifying the actual last file
- incorrect tool routing when the real artifact is unavailable

This affects more than `.docx`. The same architectural gap exists for:

- Word documents
- PowerPoint decks
- spreadsheets and tabular outputs
- generated images
- diagrams
- code-generated files
- any future artifact type where users expect iterative follow-up editing

## Core Diagnosis

The primary issue for the original invoice failure was not that COSMIC ignored prior artifact references. The exact bug was a staging extraction mismatch.

### Immediate Bug That Was Fixed

For the concrete `.docx` incident:

- `artifact_lookup` returned artifacts under `results`
- staging extraction only inspected `artifacts`
- the artifact was found but not staged
- the next-turn code execution sandbox did not receive the real file

That was the immediate root cause of the failure.

### Broader Architectural Gap

The system does remember prior artifacts at the conversation/session level. It can often:

- name them
- search for them
- reason about them
- recall what edits happened conceptually

The primary issue is that prior-turn artifact references are not being reliably converted back into usable edit inputs for later turns.

In other words:

- memory of the edit exists
- artifact identity often exists
- binary continuity is unreliable

That broader pattern is still real, but it should not be confused with the exact immediate bug above.

## Goals

1. Make follow-up file edits deterministic.
2. Make artifact reuse artifact-first, not history-first.
3. Support cross-session follow-up editing without loading an entire old session into prompt context.
4. Support manual user re-uploads as new versions in the same logical file family.
5. Generalize the fix across all artifact types without forcing every specialist to reinvent lineage and retrieval logic.
6. Add safe failure behavior: if the real artifact cannot be reopened, do not silently reconstruct a lossy replacement unless the user explicitly asks for regeneration.

## Non-Goals

1. This plan does not require implementing text-diff semantics for Office files.
2. This plan does not require a git-style low-level binary diff store.
3. This plan does not require replacing current specialists.
4. This plan does not require full historical session replay for normal follow-up edits.

## Design Principle

The retrieval unit for follow-up editing should be the artifact family, not the session and not the task.

That means:

- a task is provenance
- a session is conversational context
- an artifact family is the object the user is actually referring to

When a user says:

- "edit the invoice you changed before"
- "change the certifier on that doc"
- "update the slide deck you made last week"
- "revise the chart image you generated yesterday"

the system should first resolve the referenced artifact family, then reopen the latest appropriate artifact version inside that family.

## Proposed Architecture

### 1. Artifact Family And Version Lineage

Introduce a first-class artifact lineage model.

Each artifact should have:

- `artifact_id`
- `family_id`
- `version_id`
- `parent_artifact_ids`
- `created_by_task_id`
- `created_by_agent_id`
- `created_at`
- `artifact_type`
- `mime_type`
- `filename`
- `logical_title`
- `session_id`
- `channel`
- `origin_kind`
  - uploaded
  - produced
  - reuploaded_revision
  - derived
- `head_state`
  - active
  - superseded
  - archived

Each logical document/image/table/deck should belong to an artifact family.

Examples:

- original invoice upload = family `F1`, version `V1`
- COSMIC-edited invoice = family `F1`, version `V2`
- user manually edits and reuploads = family `F1`, version `V3`
- COSMIC edits again = family `F1`, version `V4`

The family must maintain a current head pointer, while preserving the full ancestry graph.

### 2. Artifact-First Follow-Up Resolution

For follow-up edit requests, the orchestrator should resolve inputs in this order:

1. explicit artifact attached in the current turn
2. explicit artifact id mentioned or selected
3. latest produced/editable artifact bound to the immediately previous relevant assistant response
4. latest head of a matching artifact family
5. only if confidence is low, ask the user to choose among candidate versions

It should not default to:

- full session history load
- docs bundle reads
- text reconstruction

unless the actual binary/file cannot be resolved and no safe candidate exists.

### 3. Version-Aware Reuploads

If a user manually edits a file and reuploads it, that upload should not become an unrelated artifact.

Instead, COSMIC should detect or allow linking:

- filename similarity
- prior family selection from the user
- explicit "this is the file you edited before"
- close temporal relationship to a known artifact family

The reupload should become a new family version with:

- `origin_kind = reuploaded_revision`
- parent pointing to the previous known version

This is essential because a later follow-up should target the latest real working version, not an outdated COSMIC-generated snapshot.

### 4. Shared Artifact Lineage Layer, Specialist-Specific Edit Semantics

This fix should be shared across artifact types.

Shared platform responsibilities:

- artifact identity
- lineage graph
- family/head resolution
- provenance linking
- cross-session retrieval
- artifact lookup APIs
- safe reopening of actual binary/file artifacts

Specialist-specific responsibilities:

- how to edit a Word document
- how to edit a slide deck
- how to edit a spreadsheet/table
- how to edit an image
- how to edit a diagram

This keeps the architecture clean:

- one artifact continuity model
- many specialist edit executors

### 5. Safe Fallback Rules

If a follow-up edit references a prior artifact and the real artifact cannot be reopened:

1. do not silently reconstruct a replacement from parsed text/history
2. do not route to irrelevant tools
3. do not pretend continuity exists when the binary is missing
4. instead:
   - retry artifact family/head resolution
   - retry artifact redelivery/opening
   - if still unavailable, tell the user the actual file could not be reopened
   - optionally ask for re-upload or version selection

This is critical. Silent reconstruction is worse than a clean failure because it can corrupt layout, formatting, or fidelity without making that obvious.

## Immediate Fixes

These are the smallest fixes that should land first. The first one is already completed.

### Fix A: Stage `artifact_lookup` Results For Follow-Up Edits (Completed)

Completed behavior:

- when a tool result payload contains `results` instead of `artifacts`
- the staging extractor still recognizes artifact entries
- those files are staged into the follow-up code execution sandbox

This fixed the exact invoice follow-up failure described at the top of this document.

### Fix B: Same-Session Follow-Up Artifact Reuse

When the user follows up on a freshly produced editable artifact in the next turn:

- automatically bind the latest produced artifact into the new request as an input artifact candidate
- prioritize it for edit intents
- mount or stage the real file before specialist/code-execution editing begins

This is still useful even after the `artifact_lookup.results` staging fix, because it reduces routing ambiguity and avoids unnecessary lookup/recovery work.

### Fix C: Block Lossy Reconstruction For Binary Follow-Up Edits

If the user asks to edit a prior `.docx`, `.pptx`, `.xlsx`, image, or other real artifact:

- do not rebuild from session summary or parsed markdown unless the user explicitly requests regeneration

### Fix D: Tighten Tool Routing

For local artifact follow-up editing:

- disallow irrelevant web tools such as `firecrawl.scrape`
- use document parsers only for read/inspect support
- use artifact resolution plus the correct edit path as the primary route

## Long-Term Fixes

### 1. Artifact Family Resolver

Add a shared resolver that can answer:

- what artifact family best matches this user reference?
- what is the current family head?
- what prior versions exist?
- which versions were user reuploads vs COSMIC outputs?

### 2. Edit Journal

Every editable artifact family should have a compact edit journal.

Each journal entry should record:

- task id
- timestamp
- actor
  - user upload
  - user manual revision
  - specialist edit
  - orchestrator/code edit
- parent version
- resulting version
- edit summary
- optionally structured edit ops if available

This allows selective retrieval later without replaying an entire session.

### 3. Task Notebook As Secondary Provenance

Task notebooks are still useful, but they should not be the primary file continuity mechanism.

Task notebook should answer:

- what happened in this task?
- which files were inputs?
- which outputs were produced?
- what summary of work was completed?

Artifact family should answer:

- what is the current state of this file across tasks and sessions?

### 4. Multi-File Task Support

A task can touch multiple files. That is normal.

The correct model is:

- task -> many input artifacts
- task -> many output artifacts
- each output artifact belongs to one family or starts a new family

Later follow-up retrieval should resolve by artifact family, not by dumping the whole task back into context.

## Example Cases

### Case 1: Same-Session Follow-Up `.docx` Edit

Flow:

1. user uploads invoice `A`
2. COSMIC edits it and produces `B`
3. user says "change the certifier name and title"
4. system resolves `B` as latest editable head in the active family
5. system stages real `B`
6. specialist/editor modifies `B` -> `C`
7. journal records `B -> C`

### Case 2: Ten Days Later, Continue Editing

Flow:

1. user says "edit the invoice you edited previously"
2. system searches artifact families, not full session transcripts
3. system finds invoice family `F1`
4. system resolves head version `V4`
5. system reopens the real `V4` binary
6. specialist applies the edit

If ambiguous:

- "I found three versions of that invoice. Do you mean the latest one from April 5 or an earlier version?"

### Case 3: Manual Reupload In Between

Flow:

1. original upload `V1`
2. COSMIC edit `V2`
3. user manually edits and reuploads `V3`
4. COSMIC edit `V4`
5. later follow-up should target `V4` as the current head

### Case 4: One Task, Multiple Files

Flow:

1. user asks to edit invoice and packing list together
2. task uses multiple input artifacts
3. task produces multiple output artifacts
4. each output advances its own family
5. later follow-up on the invoice resolves only the invoice family

## Artifact Types Covered

This architecture should apply to all artifact types, including:

- `.docx`
- `.pptx`
- `.xlsx`
- `.csv`
- `.parquet`
- generated images
- edited images
- diagrams
- generated code/data exports
- future binary or structured artifacts

The shared continuity layer should not care whether the edit executor is:

- docs parser + docx editor
- slide specialist
- tabular agent
- image generator agent
- diagram agent

## Suggested Implementation Areas

These areas are the likely implementation points.

- `Backend/gateway/runtime.py`
  - artifact persistence
  - artifact lookup/redelivery
  - request/input artifact preparation
  - family/head resolution APIs

- `Backend/orchestrator/tools/executor.py`
  - delegate input artifact resolution
  - automatic latest-artifact reuse for follow-up edit requests

- `Backend/orchestrator/tools/registry.py`
  - clearer tool contracts for artifact follow-up editing

- task/session stores
  - add artifact family/version metadata
  - add edit journal storage

- specialist agents
  - consume resolved real input artifacts
  - emit parent-child lineage metadata on produced outputs

## Rollout Plan

### Phase 1: Stop The Bleeding

1. completed: stage `artifact_lookup.results` artifacts into follow-up execution the same way `artifact_redeliver.artifacts` already worked
2. auto-pass latest produced editable artifact into immediate follow-up edit turns
3. forbid lossy reconstruction for binary follow-up edits
4. forbid irrelevant fallback routing in local file-edit flows
5. add tests for two-turn follow-up editing

### Phase 2: Artifact Family Metadata

1. introduce family ids and parent-child version metadata
2. track family head
3. preserve provenance for uploads, reuploads, and produced outputs

### Phase 3: Edit Journal

1. store compact per-family edit logs
2. support selective retrieval of:
   - latest edit
   - all edits
   - version chain
   - edits by section/file when available

### Phase 4: Cross-Session Retrieval

1. resolve artifact families without loading full old sessions
2. support ambiguity resolution when multiple families match
3. expose concise version summaries to the orchestrator

### Phase 5: Specialist Integration

1. docs follow-up editing
2. slides follow-up editing
3. tabular follow-up editing
4. image follow-up editing
5. diagram follow-up editing

## Tests Required

### Minimum Required

1. upload `.docx` -> edit -> follow-up edit next turn uses produced artifact binary
2. upload `.pptx` -> edit -> follow-up edit next turn uses produced artifact binary
3. generated image -> edit variant -> follow-up edit uses latest image head
4. tabular file -> transform -> follow-up transform uses latest table head
5. manual reupload becomes a new version in the same family
6. multi-file task preserves separate families correctly
7. when artifact reopening fails, COSMIC cleanly reports the failure instead of reconstructing

## Acceptance Criteria

This work is successful when:

1. a follow-up edit in the next turn always reopens the real previous output artifact when appropriate
2. a follow-up edit days later resolves the correct artifact family without replaying a full old session
3. manual reuploads are treated as legitimate new family versions
4. tasks with multiple files do not confuse later follow-up retrieval
5. binary artifacts are never silently reconstructed from parsed text/history unless the user explicitly asks for regeneration
6. specialists receive real staged file inputs, not only remembered descriptions of prior outputs

## Final Recommendation

The first implementation target should remain the exact invoice follow-up failure that triggered this plan.

That means:

1. keep the completed `artifact_lookup.results` staging fix in place
2. improve immediate turn-to-turn produced artifact reuse for editable files
3. add safe failure behavior instead of reconstruction
4. then generalize into shared artifact lineage across all artifact types

That sequence gives the highest leverage with the lowest risk.
