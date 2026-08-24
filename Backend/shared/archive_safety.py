"""Inspection and extraction for user-supplied archives.

Everything here treats the archive as hostile input. An uploaded zip is the most
adversarial thing Cosmic accepts: its entry names decide where bytes land, its
declared sizes decide how much we allocate, and both are attacker-controlled.

Two entry points, deliberately separated:

  ``inspect_zip``      reads only the central directory. No decompression, no
                       writes. Cheap enough to run on every upload, which is what
                       lets the orchestrator decide what an archive *is* before
                       anything is unpacked.

  ``safe_extract_zip`` unpacks a manifest that inspection already accepted, and
                       re-validates every entry as it goes. Inspection is a
                       filter, never a permission slip.

The split matters for one specific attack: a zip's central directory is metadata
and may lie. ``file_size`` can claim 1 KB while the compressed stream expands to
gigabytes. So extraction never trusts the manifest's sizes; it streams in chunks
and counts what it actually writes.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


# ── Limits ───────────────────────────────────────────────────────────────────
# Chosen to comfortably fit a real front-end project while refusing anything
# built to exhaust the box. Every one of these is a hard stop, not a warning.

MAX_ENTRIES = 5_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ENTRY_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_PATH_DEPTH = 24
MAX_PATH_LENGTH = 1_024
MAX_NAME_LENGTH = 200

# A zip bomb's whole trick is compression ratio. Legitimate text compresses well
# (10-50x is ordinary for source), so the ratio check only engages once there is
# enough compressed data for the number to mean anything -- otherwise a 12-byte
# archive of zeroes trips it.
MAX_COMPRESSION_RATIO = 200
RATIO_CHECK_MIN_COMPRESSED_BYTES = 4 * 1024

# Manifests travel into the orchestrator's context. Cap what we describe so a
# 5,000-file archive cannot become 5,000 lines of prompt.
MANIFEST_ENTRY_SAMPLE = 200

_EXTRACT_CHUNK_BYTES = 64 * 1024

# Archive containers that are really document formats. These must never be
# treated as source bundles: .docx/.xlsx/.pptx are zips, and the document and
# spreadsheet pipelines own them.
DOCUMENT_ZIP_EXTENSIONS = frozenset(
    {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm", ".odt", ".ods", ".odp", ".epub"}
)

# Archive containers that are packaged executables. Nothing good comes of
# unpacking these into an agent workspace.
EXECUTABLE_ZIP_EXTENSIONS = frozenset(
    {".jar", ".war", ".ear", ".apk", ".aab", ".ipa", ".xpi", ".crx", ".whl", ".nupkg"}
)

# Platform litter. Filtered from the manifest so the project shape is readable,
# and skipped on extraction so it never reaches a workspace.
_CRUFT_DIR_PREFIXES = ("__MACOSX/", ".Spotlight-V100/", ".Trashes/", "$RECYCLE.BIN/")
_CRUFT_BASENAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", "._.DS_Store"})

# Windows refuses these as filenames regardless of extension. An archive
# carrying one is either broken or probing.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ArchiveEntry:
    """One accepted file, named relative to the stripped project root."""

    name: str
    size: int
    compressed_size: int
    is_dir: bool


@dataclass
class ArchiveManifest:
    """What an archive contains, and whether it may be unpacked at all."""

    ok: bool
    rejection: str | None = None
    entry_count: int = 0
    file_count: int = 0
    total_uncompressed: int = 0
    total_compressed: int = 0
    max_depth: int = 0
    common_root: str | None = None
    top_level: list[str] = field(default_factory=list)
    entries: list[ArchiveEntry] = field(default_factory=list)
    truncated: bool = False
    project_kind: str = "unknown"
    signals: list[str] = field(default_factory=list)
    skipped_cruft: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Compact form for artifact records and prompt context."""
        payload: dict[str, Any] = {
            "ok": self.ok,
            "entry_count": self.entry_count,
            "file_count": self.file_count,
            "total_uncompressed": self.total_uncompressed,
            "project_kind": self.project_kind,
        }
        if self.rejection:
            payload["rejection"] = self.rejection
        if self.common_root:
            payload["common_root"] = self.common_root
        if self.top_level:
            payload["top_level"] = self.top_level[:40]
        if self.signals:
            payload["signals"] = self.signals
        if self.truncated:
            payload["truncated"] = True
        if self.skipped_cruft:
            payload["skipped_cruft"] = self.skipped_cruft
        return payload


class ArchiveRejected(Exception):
    """Raised by safe_extract_zip when an entry fails re-validation."""


# ── Name handling ────────────────────────────────────────────────────────────


def _is_cruft(name: str) -> bool:
    if any(name.startswith(prefix) for prefix in _CRUFT_DIR_PREFIXES):
        return True
    base = posixpath.basename(name.rstrip("/"))
    if base in _CRUFT_BASENAMES:
        return True
    # AppleDouble sidecars: ._Foo alongside Foo.
    return base.startswith("._")


def normalize_entry_name(raw_name: str) -> str | None:
    """Return a safe relative posix path, or None if the entry must be refused.

    This is the single chokepoint for zip-slip. Everything downstream assumes a
    name that survived this function cannot escape its destination.
    """
    name = str(raw_name or "")
    if not name:
        return None
    if _CONTROL_CHARS.search(name):
        return None
    if len(name) > MAX_PATH_LENGTH:
        return None

    # Some Windows tooling writes backslash separators despite the spec.
    name = name.replace("\\", "/")

    # Drive letters and UNC paths are absolute even without a leading slash.
    if re.match(r"^[A-Za-z]:", name) or name.startswith("//"):
        return None
    if name.startswith("/"):
        return None

    is_dir = name.endswith("/")
    parts: list[str] = []
    for part in PurePosixPath(name).parts:
        if part in ("", "."):
            continue
        if part == "..":
            # No amount of later normalisation makes this safe.
            return None
        if len(part) > MAX_NAME_LENGTH:
            return None
        # Windows refuses trailing dots/spaces and reserved device names; an
        # archive containing them cannot be materialised faithfully anywhere.
        if part != part.rstrip(". "):
            return None
        if part.split(".", 1)[0].lower() in _WINDOWS_RESERVED:
            return None
        parts.append(part)

    if not parts:
        return None
    if len(parts) > MAX_PATH_DEPTH:
        return None

    normalized = "/".join(parts)
    return f"{normalized}/" if is_dir else normalized


def _entry_file_type(info: zipfile.ZipInfo) -> int:
    """The S_IFMT bits of the entry's unix mode, or 0 when none were recorded.

    Unix mode lives in the top 16 bits of external_attr, but plenty of writers
    (including zipfile.writestr) store permission bits only, leaving the type
    bits zero. Those entries carry no type claim at all and must not be read as
    though they claimed something exotic.
    """
    return (info.external_attr >> 16) & 0o170000


def _entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    return _entry_file_type(info) == stat.S_IFLNK


def _entry_is_regular_or_dir(info: zipfile.ZipInfo) -> bool:
    """Refuse devices, fifos and sockets; only files and directories may pass."""
    file_type = _entry_file_type(info)
    if not file_type:
        return True  # No type recorded, which is the common case.
    return file_type in (stat.S_IFREG, stat.S_IFDIR)


def _is_encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & 0x1)


# ── Project shape ────────────────────────────────────────────────────────────


def _compute_common_root(names: Iterable[str]) -> str | None:
    """The single wrapper directory GitHub-style zips add, if there is one."""
    roots: set[str] = set()
    saw_root_file = False
    for name in names:
        head, _, tail = name.partition("/")
        if not tail:
            saw_root_file = True
        roots.add(head)
        if len(roots) > 1:
            return None
    if saw_root_file or len(roots) != 1:
        return None
    return next(iter(roots))


def _detect_project(file_names: set[str]) -> tuple[str, list[str]]:
    """Classify by the marker files a developer would look for first."""
    signals: list[str] = []

    def has(name: str) -> bool:
        present = name in file_names
        if present:
            signals.append(name)
        return present

    has_git = any(name.startswith(".git/") for name in file_names)
    if has_git:
        signals.append(".git/")

    node = has("package.json")
    python = any(
        has(marker) for marker in ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile")
    )
    rust = has("Cargo.toml")
    go = has("go.mod")
    root_index = has("index.html")
    any_html = any(name.lower().endswith((".html", ".htm")) for name in file_names)

    if node:
        kind = "node_project"
    elif python:
        kind = "python_project"
    elif rust:
        kind = "rust_project"
    elif go:
        kind = "go_project"
    elif root_index:
        kind = "static_site"
    elif any_html:
        kind = "web_files"
        signals.append("html files")
    elif file_names:
        kind = "files"
    else:
        kind = "unknown"
    return kind, signals


# ── Inspection ───────────────────────────────────────────────────────────────


def _reject(message: str) -> ArchiveManifest:
    return ArchiveManifest(ok=False, rejection=message)


def inspect_zip(path: Path | str) -> ArchiveManifest:
    """Describe an archive without decompressing or writing anything.

    Reads the central directory only, so cost is proportional to entry count
    rather than payload size -- which is exactly what makes it safe to run on a
    zip bomb.
    """
    archive_path = Path(path)
    if not archive_path.is_file():
        return _reject("Archive file is missing.")

    suffix = archive_path.suffix.lower()
    if suffix in DOCUMENT_ZIP_EXTENSIONS:
        return _reject(
            f"{suffix} is a document format, not a source bundle. It belongs to the document pipeline."
        )
    if suffix in EXECUTABLE_ZIP_EXTENSIONS:
        return _reject(f"{suffix} is a packaged executable and is not unpacked into a workspace.")

    if not zipfile.is_zipfile(archive_path):
        return _reject("File is not a valid zip archive.")

    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        return _reject(f"Archive could not be read: {exc}")

    if not infos:
        return _reject("Archive is empty.")
    if len(infos) > MAX_ENTRIES:
        return _reject(
            f"Archive has {len(infos)} entries, above the {MAX_ENTRIES} limit."
        )

    entries: list[ArchiveEntry] = []
    total_uncompressed = 0
    total_compressed = 0
    max_depth = 0
    skipped_cruft = 0

    for info in infos:
        if _is_encrypted(info):
            return _reject("Archive is password-protected and cannot be unpacked.")
        if _entry_is_symlink(info):
            return _reject(f"Archive contains a symlink ({info.filename}), which is not unpacked.")
        if not _entry_is_regular_or_dir(info):
            return _reject(f"Archive contains a special file ({info.filename}).")

        normalized = normalize_entry_name(info.filename)
        if normalized is None:
            return _reject(f"Archive contains an unsafe path: {info.filename!r}")
        if _is_cruft(normalized):
            skipped_cruft += 1
            continue

        is_dir = normalized.endswith("/") or info.is_dir()
        clean = normalized.rstrip("/")
        depth = clean.count("/") + 1
        max_depth = max(max_depth, depth)

        if is_dir:
            entries.append(ArchiveEntry(name=f"{clean}/", size=0, compressed_size=0, is_dir=True))
            continue

        if info.file_size > MAX_ENTRY_UNCOMPRESSED_BYTES:
            return _reject(
                f"Archive entry {clean} expands to "
                f"{info.file_size // (1024 * 1024)} MB, above the per-file limit."
            )
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            return _reject(
                f"Archive expands to more than "
                f"{MAX_TOTAL_UNCOMPRESSED_BYTES // (1024 * 1024)} MB."
            )
        entries.append(
            ArchiveEntry(
                name=clean,
                size=info.file_size,
                compressed_size=info.compress_size,
                is_dir=False,
            )
        )

    file_entries = [item for item in entries if not item.is_dir]
    if not file_entries:
        return _reject("Archive contains no usable files.")

    if (
        total_compressed >= RATIO_CHECK_MIN_COMPRESSED_BYTES
        and total_uncompressed > total_compressed * MAX_COMPRESSION_RATIO
    ):
        return _reject(
            f"Archive compression ratio "
            f"({total_uncompressed // max(total_compressed, 1)}:1) looks like a zip bomb."
        )

    common_root = _compute_common_root(item.name for item in entries)
    if common_root:
        prefix = f"{common_root}/"
        stripped: list[ArchiveEntry] = []
        for item in entries:
            if not item.name.startswith(prefix):
                continue
            trimmed = item.name[len(prefix) :]
            if not trimmed.rstrip("/"):
                continue
            stripped.append(
                ArchiveEntry(
                    name=trimmed,
                    size=item.size,
                    compressed_size=item.compressed_size,
                    is_dir=item.is_dir,
                )
            )
        entries = stripped
        file_entries = [item for item in entries if not item.is_dir]
        max_depth = max((item.name.rstrip("/").count("/") + 1) for item in entries) if entries else 0

    file_names = {item.name for item in file_entries}
    project_kind, signals = _detect_project(file_names)

    top_level = sorted(
        {item.name.split("/", 1)[0] + ("/" if "/" in item.name.rstrip("/") or item.is_dir else "")
         for item in entries}
    )

    sample = entries[:MANIFEST_ENTRY_SAMPLE]
    return ArchiveManifest(
        ok=True,
        entry_count=len(entries),
        file_count=len(file_entries),
        total_uncompressed=total_uncompressed,
        total_compressed=total_compressed,
        max_depth=max_depth,
        common_root=common_root,
        top_level=top_level,
        entries=sample,
        truncated=len(entries) > len(sample),
        project_kind=project_kind,
        signals=signals,
        skipped_cruft=skipped_cruft,
    )


# ── Extraction ───────────────────────────────────────────────────────────────


def safe_extract_zip(
    path: Path | str,
    destination: Path | str,
    *,
    strip_common_root: bool = True,
) -> list[str]:
    """Unpack an archive under ``destination``, re-validating every entry.

    Returns the relative paths written. Raises ArchiveRejected on anything that
    fails validation, leaving the destination partially populated -- callers
    extract into a scratch directory and discard it on failure.
    """
    archive_path = Path(path)
    dest_root = Path(destination).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    manifest = inspect_zip(archive_path)
    if not manifest.ok:
        raise ArchiveRejected(manifest.rejection or "Archive was refused.")

    prefix = f"{manifest.common_root}/" if (strip_common_root and manifest.common_root) else ""
    written: list[str] = []
    budget = MAX_TOTAL_UNCOMPRESSED_BYTES

    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            # Re-derive rather than reuse the manifest: the manifest is a
            # filter's opinion, not a capability.
            normalized = normalize_entry_name(info.filename)
            if normalized is None:
                raise ArchiveRejected(f"Unsafe path in archive: {info.filename!r}")
            if _is_cruft(normalized):
                continue
            if _is_encrypted(info) or _entry_is_symlink(info) or not _entry_is_regular_or_dir(info):
                raise ArchiveRejected(f"Refused archive entry: {info.filename!r}")

            relative = normalized
            if prefix:
                if not relative.startswith(prefix):
                    continue
                relative = relative[len(prefix) :]
            clean = relative.rstrip("/")
            if not clean:
                continue

            target = (dest_root / clean).resolve()
            # Belt and braces: normalize_entry_name already forbids traversal,
            # but the final containment check is what actually guarantees it.
            if not target.is_relative_to(dest_root):
                raise ArchiveRejected(f"Archive entry escapes the destination: {clean}")

            if normalized.endswith("/") or info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            entry_written = 0
            with archive.open(info, "r") as source, open(target, "wb") as sink:
                while True:
                    chunk = source.read(_EXTRACT_CHUNK_BYTES)
                    if not chunk:
                        break
                    entry_written += len(chunk)
                    budget -= len(chunk)
                    # The central directory's declared size is metadata and can
                    # lie; what the stream actually produces is the only number
                    # worth enforcing.
                    if entry_written > MAX_ENTRY_UNCOMPRESSED_BYTES or budget < 0:
                        sink.close()
                        target.unlink(missing_ok=True)
                        raise ArchiveRejected(
                            f"Archive entry {clean} expanded past its declared size."
                        )
                    sink.write(chunk)
            # Never carry the archive's permission bits: no entry arrives executable.
            target.chmod(0o644)
            written.append(clean)

    if not written:
        raise ArchiveRejected("Archive produced no files.")
    return written


def extract_zip_to_staging(
    path: Path | str,
    staging_root: Path | str,
) -> tuple[Path, list[str]]:
    """Extract into a fresh directory, cleaning up if anything is refused."""
    target = Path(staging_root)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    try:
        written = safe_extract_zip(path, target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target, written
