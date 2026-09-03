# Alpha Agent Changelog

## Unreleased

- Added ZCode as a fourth Alpha harness (Z.ai's official GLM agent): headless `zcode --prompt --mode yolo --json` runs with session resume, `ZCODE_MODEL` per-run model override, and the GLM-5.3 / GLM-5.3-Flash pair with the low/high/max thinking ladder (`zcode_runner.py`, `shared/zcode_cli.py`).
- ZCode auth follows the CLI's own model: `zcode login` (Z.ai OAuth) or a pasted API key lands in the ZCode home's `.zcode/cli/config.json`; the gateway mirrors status/model/thinking in `agent_provider_auth` and never duplicates the key.
- Bootstrap installs the CLI by extracting it from the official desktop AppImage (`--appimage-extract`), with a dedicated Node >= 22.13 runtime under `/usr/local/lib/cosmic/zcode` when the system Node is too old for `node:sqlite`.

- Added connected-GitHub repository sync: Alpha clones each connected repo to one canonical checkout (`ALPHA_REPOS_ROOT/<owner>/<name>`), ensures it is in sync (`clone` / `fetch` + `--ff-only` when clean and strictly behind) before running a harness, runs the harness inside the checkout, and reports branch/last-commit/ahead-behind progress back to the Gateway (`repo_sync.py`).
- Repo-backed tasks skip the workspace `AGENTS.md` seed and stage inputs into the artifacts dir, so COSMIC never dirties a user's repository.
- Added `github_repo_search` awareness to the CLI prompt and global instructions: re-verify sync (`git status` / `git fetch` / compare to `origin/`) before editing, never force-push or amend pushed history, and push only when the goal explicitly asks.

## 1.0.0 - 2026-05-02

- Added Alpha Agent V1 scaffold.
- Added project registry, workspace manager, and Docker workspace runner boundary.
- Added workspace-preparation-only `alpha.execute` and `alpha.recall_project` intents.

