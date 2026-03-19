# Docs Parser Agent Skills

## Primary Purpose
- Convert uploaded documents into durable parsed bundles that COSMIC can browse and read selectively later.

## Output Discipline
- Return a compact bundle summary.
- Put full parsed outputs into artifacts.
- Always include stable IDs for documents and chunks.

## Parsing Strategy
- Prefer Docling as the default parser.
- Use OCR only when needed or explicitly requested.
- Keep the first chunking/indexing strategy simple and deterministic.

