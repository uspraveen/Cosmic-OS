# Repository Guidelines

## Project Structure & Module Organization
`run_local.py` is the end-to-end entrypoint. It orchestrates four stages: `template_cataloger.py`, `content_planner.py`, `layout_selector.py`, and `slide_builder.py`. `asset_manager.py` handles icon/photo lookup and caching. Keep stage-specific logic inside its owning module instead of spreading it across the pipeline.

`templates/` stores source `.pptx` templates. `catalogs/<template_name>/` holds generated template metadata, thumbnail grids, and per-slide previews. `assets/cache/` stores downloaded icons, photos, and charts. `output/` and `output_*/` contain generated decks, PDFs, slide PNGs, and `build_report.json`.

## Build, Test, and Development Commands
`python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt` installs local dependencies.

`python run_local.py -d "5 slides about AI in healthcare" -t templates/Startup_pitch_deck.pptx -o output --validate` runs the full pipeline and renders validation artifacts.

`python template_cataloger.py templates/Startup_pitch_deck.pptx --force --verbose` rebuilds a template catalog from scratch.

`python slide_builder.py output/build_spec.json templates/Startup_pitch_deck.pptx -o output --validate` reruns only the builder when iterating on rendering.

`python -m compileall -q .` is the fastest repo-wide syntax smoke test.

## Coding Style & Naming Conventions
Use 4-space indentation, module docstrings, and `snake_case` for functions, variables, and JSON keys. Reserve `UPPER_SNAKE_CASE` for configuration constants sourced from `.env`. Prefer `Path`, explicit type hints, and `logging` over ad hoc path strings or scattered `print()` debugging.

## Testing Guidelines
There is no dedicated `tests/` package yet. For every change, run `python -m compileall -q .`. For pipeline-affecting changes, also run one end-to-end build and inspect `output/slides/*.png` plus `output/build_report.json`. If you add automated tests, place them under `tests/` with `test_*.py` names and keep fixtures small.

## Commit & Pull Request Guidelines
Current history is minimal and not very descriptive (`Uploads`, `bom`), so use clear imperative subjects such as `builder: tighten repair loop`. Keep commits focused to one stage or behavior change. PRs should include the verification command used, note any `.env` or external dependency changes, and attach screenshots or PNG diffs when slide output changes.

## Security & Configuration Tips
Do not commit real API keys, local absolute paths, or generated artifacts. Keep machine-specific settings such as `LIBREOFFICE_PATH` and `PDFTOPPM_PATH` in `.env`, and review changes to `catalogs/`, `assets/cache/`, and `output/` before committing.
