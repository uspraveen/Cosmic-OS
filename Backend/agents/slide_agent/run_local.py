"""Unified local runner for cosmic-slides-2.

Supports two backends:
  1. template  → native PowerPoint template filling (existing slides-2 flow)
  2. html      → HTML/CSS design-first rendering (integrated slides-3 flow)

Routing defaults:
  - template provided → template backend
  - no template       → html backend

Usage
─────
  python run_local.py --description "5 slides about AI in healthcare" --template templates/Startup_pitch_deck.pptx
  python run_local.py --description "Chennai culture and economy" --template templates/Startup_pitch_deck.pptx --max-slides 8
  python run_local.py --description "Quarterly review" --template templates/Startup_pitch_deck.pptx --validate
  python run_local.py --description "A cinematic deck on India's EV transition" --workflow html --validate
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from template_cataloger import catalog_template, load_catalog
from slide_builder import run_deck_builder
from html_workflow import run_html_pipeline

logger = logging.getLogger("run_local")


def run_template_pipeline(
    description: str,
    template_path: Path,
    *,
    max_slides: int | None = None,
    output_dir: Path = Path("output"),
    validate: bool = False,
    force_catalog: bool = False,
) -> dict:
    """Run the original native template pipeline end-to-end."""
    output_dir = output_dir.resolve()

    t0 = time.time()

    # ── Stage 1: Catalog ──────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("STAGE 1 / 4 — Template Cataloger")
    logger.info("=" * 60)

    catalog = load_catalog(template_path.resolve())
    if catalog is None or force_catalog:
        logger.info("Generating catalog for '%s' …", template_path.name)
        catalog = catalog_template(template_path)
    else:
        logger.info("Using cached catalog for '%s'", template_path.name)

    logger.info("  Template: %s (%d slides)", catalog["template_name"], catalog["slide_count"])
    t1 = time.time()
    logger.info("  Time: %.1fs", t1 - t0)

    logger.info("")
    logger.info("=" * 60)
    logger.info("STAGE 2-4 / 4 — Builder Graph")
    logger.info("=" * 60)
    result = run_deck_builder(
        description,
        template_path,
        output_dir,
        template_catalog=catalog,
        max_slides=max_slides,
        validate=validate,
    )

    build_spec = result.get("build_spec", {})
    t4 = time.time()
    logger.info("  Time: %.1fs", t4 - t1)

    # ── Summary ───────────────────────────────────────────────────────────
    total = t4 - t0
    print(f"\n{'=' * 60}")
    print(f"  COSMIC-SLIDES-2 — Build Complete")
    print(f"{'=' * 60}")
    print(f"  Deck:     {build_spec.get('deck_title', '?')}")
    print(f"  Template: {catalog['template_name']}")
    print(f"  Slides:   {len(build_spec['slides'])}")
    print(f"  PPTX:     {result.get('pptx_path', 'n/a')}")
    pngs = result.get("slide_pngs", [])
    if pngs:
        print(f"  PNGs:     {len(pngs)} -> {Path(pngs[0]).parent}")
    val = result.get("validation_results", [])
    if val:
        passed = sum(1 for v in val if v.get("verdict") == "pass")
        print(f"  Validation: {passed}/{len(val)} passed")
    errs = result.get("errors", [])
    if errs:
        print(f"  Errors:   {len(errs)}")
        for e in errs[:5]:
            print(f"    - {e}")
    print(f"  Total time: {total:.1f}s")
    print(f"{'=' * 60}\n")

    result.setdefault("workflow", "template")
    return result


def run_pipeline(
    description: str,
    template_path: Path,
    *,
    max_slides: int | None = None,
    output_dir: Path = Path("output"),
    validate: bool = False,
    force_catalog: bool = False,
) -> dict:
    """Backward-compatible alias for the original template pipeline."""
    return run_template_pipeline(
        description=description,
        template_path=template_path,
        max_slides=max_slides,
        output_dir=output_dir,
        validate=validate,
        force_catalog=force_catalog,
    )


def run_orchestrated_pipeline(
    description: str,
    *,
    workflow: str = "auto",
    template_path: Path | None = None,
    max_slides: int | None = None,
    output_dir: Path = Path("output"),
    validate: bool = False,
    force_catalog: bool = False,
) -> dict:
    """Route to the native template or HTML workflow without mixing their logic."""
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    selected_workflow = workflow
    if selected_workflow == "auto":
        selected_workflow = "template" if template_path else "html"

    logger.info("workflow: %s", selected_workflow)

    if selected_workflow == "template":
        if template_path is None:
            raise ValueError("Template workflow requires --template.")
        return run_template_pipeline(
            description=description,
            template_path=template_path,
            max_slides=max_slides,
            output_dir=output_dir,
            validate=validate,
            force_catalog=force_catalog,
        )

    if selected_workflow == "html":
        if template_path is not None:
            logger.info("html workflow ignores the provided template path: %s", template_path)
        result = run_html_pipeline(
            description,
            output_dir=output_dir,
            max_slides=max_slides,
            validate=validate,
        )
        result.setdefault("workflow", "html")
        return result

    raise ValueError(f"Unknown workflow: {workflow}")


def _print_html_summary(result: dict, output_dir: Path) -> None:
    print(f"\n{'=' * 68}")
    print("  COSMIC-SLIDES-2 — HTML WORKFLOW COMPLETE")
    print(f"{'=' * 68}")
    print(f"  Output:  {output_dir.resolve()}")
    if result.get("pptx_path"):
        print(f"  PPTX:    {result['pptx_path']}")
    slide_pngs = result.get("slide_pngs") or []
    if slide_pngs:
        print(f"  PNGs:    {len(slide_pngs)} -> {Path(slide_pngs[0]).parent}")
    if result.get("pdf_path"):
        print(f"  PDF:     {result['pdf_path']}")
    if result.get("contact_sheet"):
        print(f"  Sheet:   {result['contact_sheet']}")
    validation_results = result.get("validation_results") or []
    if validation_results:
        passed = sum(1 for item in validation_results if item.get("verdict") == "pass")
        print(f"  Validation: {passed}/{len(validation_results)} passed")
    print(f"{'=' * 68}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run cosmic-slides-2 using the native template backend or the integrated HTML backend."
    )
    parser.add_argument("--description", "-d", required=True,
                        help="What the presentation should be about.")
    parser.add_argument("--workflow", choices=["auto", "template", "html"], default="auto",
                        help="Choose rendering backend. Default: auto (template if --template is given, else html).")
    parser.add_argument("--template", "-t", type=Path,
                        help="Optional path to the .pptx template file.")
    parser.add_argument("--max-slides", "-n", type=int, default=None,
                        help="Maximum number of slides in output deck.")
    parser.add_argument("--out", "-o", type=Path, default=Path("output"),
                        help="Output directory (default: ./output).")
    parser.add_argument("--validate", action="store_true",
                        help="Run visual validation on rendered slides.")
    parser.add_argument("--force-catalog", action="store_true",
                        help="Force re-catalog the template even if cached.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-5s  %(message)s",
    )

    if args.template and not args.template.exists():
        print(f"Error: template not found: {args.template}", file=sys.stderr)
        sys.exit(1)

    try:
        result = run_orchestrated_pipeline(
            description=args.description,
            workflow=args.workflow,
            template_path=args.template.resolve() if args.template else None,
            max_slides=args.max_slides,
            output_dir=args.out,
            validate=args.validate,
            force_catalog=args.force_catalog,
        )
        resolved_workflow = result.get("workflow")
        if resolved_workflow == "html":
            _print_html_summary(result, args.out)
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
