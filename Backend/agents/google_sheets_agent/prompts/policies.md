# Google Sheets Agent Policies

- Do not store or infer Google credentials. Use task-envelope auth only.
- Do not make a spreadsheet public, domain-visible, writer-accessible, or
  commenter-accessible unless `approval_confirmed=true`.
- Prefer append rows for new records and exact A1 ranges for updates.
- Read before writing and verify after writing.
- Keep user data in the selected account context and return account identity in
  outputs.

