from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def connect_sync(path: str | Path, *, row_factory: Any = sqlite3.Row) -> sqlite3.Connection:
    """Open a SQLite database with the repo's standard pragmas."""
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = row_factory
    connection.execute("PRAGMA foreign_keys=ON;")
    connection.execute("PRAGMA busy_timeout = 5000;")
    connection.execute("PRAGMA journal_mode=WAL;")
    return connection
