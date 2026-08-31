# Alpha Agent Changelog

## Unreleased

- Added connected-GitHub repository sync: Alpha clones each connected repo to one canonical checkout (`ALPHA_REPOS_ROOT/<owner>/<name>`), ensures it is in sync (`clone` / `fetch` + `--ff-only` when clean and strictly behind) before running a harness, runs the harness inside the checkout, and reports branch/last-commit/ahead-behind progress back to the Gateway (`repo_sync.py`).
- Repo-backed tasks skip the workspace `AGENTS.md` seed and stage inputs into the artifacts dir, so COSMIC never dirties a user's repository.
- Added `github_repo_search` awareness to the CLI prompt and global instructions: re-verify sync (`git status` / `git fetch` / compare to `origin/`) before editing, never force-push or amend pushed history, and push only when the goal explicitly asks.

## 1.0.0 - 2026-05-02

- Added Alpha Agent V1 scaffold.
- Added project registry, workspace manager, and Docker workspace runner boundary.
- Added workspace-preparation-only `alpha.execute` and `alpha.recall_project` intents.

