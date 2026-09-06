"""Repro: full advanced pipeline with QA enabled — catches the streaming crash."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, "agents/slide_agent")

from native_workflow import run_native_pipeline  # noqa: E402

try:
    result = run_native_pipeline(
        "Make a 3 slide deck about TriZ AI, black background with teal accents.",
        output_dir=Path("/tmp/native_repro2"),
        max_slides=3,
        validate=True,
    )
    print("PIPELINE_OK", result.get("pptx_path"))
except Exception:
    traceback.print_exc()
