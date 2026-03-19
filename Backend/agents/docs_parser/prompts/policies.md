# Policies

## Error Handling
- Return `INVALID_INPUT` for malformed requests, empty artifact lists, or unsupported option values.
- Return `UNSUPPORTED_ARTIFACT` for non-document artifact types or unsupported MIME/file combinations.
- Return `MISSING_ARTIFACT` if an input artifact path is missing or fails integrity validation.
- Return `PARSER_UNAVAILABLE` if Docling is not installed or importable in the runtime.
- Return `PARSE_FAILED` when the parser cannot convert a valid input document.
- Return `INTERNAL_ERROR` for unexpected local persistence or serialization failures.

## Parsing Rules
- Parse each input document into one canonical parsed bundle.
- `document.json` is the canonical parsed truth.
- `document.md` is the model-facing linearized read surface.
- `chunk_index.json` must be generated even if the first chunking strategy is simple.
- Preserve asset references whenever available.

## Storage Rules
- Store all full outputs under `runs/artifacts/<task_id>/docs_parser/<artifact_id>/`.
- Keep only compact per-task summaries in the private session ledger.
- Do not write giant parsed documents into shared memory.

