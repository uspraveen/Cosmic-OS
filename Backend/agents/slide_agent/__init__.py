"""COSMIC Slide Agent adapter over the copied cosmic-slides-2 core."""

# The slides core modules (llm_client, html_workflow, slide_builder, …) use
# bare sibling imports so they can run standalone from this directory. Make
# package-mode imports (agents.slide_agent.asset_manager, tests) resolve them
# too, regardless of which submodule is imported first.
import sys
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parent
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))
