"""
Wrapper: run the MiMo LangChain smoke test from the repo root.

The implementation lives at Backend/scripts/local_test_mimo_langchain.py

Usage (from Cosmic-OS/):
  python scripts/local_test_mimo_langchain.py

Or with full path:
  python "C:\\...\\Cosmic-OS\\Backend\\scripts\\local_test_mimo_langchain.py"
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_SCRIPT = _ROOT / "Backend" / "scripts" / "local_test_mimo_langchain.py"


def main() -> None:
    if not _BACKEND_SCRIPT.is_file():
        print(f"Expected script not found: {_BACKEND_SCRIPT}", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(subprocess.call([sys.executable, str(_BACKEND_SCRIPT)]))


if __name__ == "__main__":
    main()
