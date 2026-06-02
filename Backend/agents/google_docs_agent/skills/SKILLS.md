# Google Docs Agent Skills

- Resolve Google Docs resources across the selected account's Drive.
- Use an internal LLM planner for high-level requests, document restructuring,
  ambiguous edits, share/comment intent normalization, and safe operation choice.
- Read document outlines, full text, block IDs, tables, images, and comments.
- Create documents from Markdown-like source text, including defensive conversion
  of markdown pipe-table blocks into native Google Docs tables.
- Perform revision-guarded edits: overwrite, replace text, update block,
  insert table, insert image, comments, replies, resolve/reopen comments, share.
- For tracker docs, status keys, priority keys, schedules, contact lists, and
  comparison matrices, prefer native Docs tables with styled headers over prose
  or literal markdown table text.
- Maintain a private edit/session ledger for recall and audit.
