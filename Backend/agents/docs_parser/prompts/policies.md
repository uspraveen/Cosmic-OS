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
- For DOCX, standard parsing should run first. If the result is image-heavy or structurally weak, Office render plus hosted full-page VLM escalation may be applied.
- For PPTX, treat the document as visual-first by default. In auto mode, Office render plus hosted full-page VLM should run unless the operator explicitly disables that path.
- When an exact figure, chart, page image, or slide image needs a more faithful read, use the hosted asset-reinspection path and cache the result under the parsed bundle.
- If picture description, Office render, or hosted full-page VLM enrichment fails, keep the best standard parse bundle and record the fallback reason instead of inventing content or failing silently.

## Storage Rules
- Store all full outputs under `runs/artifacts/<task_id>/docs_parser/<artifact_id>/`.
- Keep only compact per-task summaries in the private session ledger.
- Do not write giant parsed documents into shared memory.
