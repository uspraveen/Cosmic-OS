"""COSMIC Browser Agent adapter over the cosmic-browser-use core."""

# The cosmic-browser-use checkout uses bare top-level imports (main,
# browser_controller, browser_memory, ...) so it can run standalone. The
# specialist adds its checkout directory to sys.path at construction time
# (BrowserAgentConfig.ensure_import_path) — nothing to do here, but keep a
# module docstring anchor for tests.
