from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Iterable

from shared.contracts import ArtifactManifest

from .workspace_manager import WorkspacePaths


CREATED_BY = "cosmic/alpha-agent:1.0.0"
MAX_PROMOTED_ARTIFACTS = 24
MAX_PROMOTED_FILE_BYTES = 512 * 1024 * 1024
# CLI runner transcripts, research copies, and deployment notes are internal — not user deliverables.
INTERNAL_ARTIFACT_FILENAMES = frozenset(
    {
        "cursor-last-message.md",
        "codex-last-message.md",
        "alpha-full-goal.md",
        "DEPLOYMENT_REPORT.md",
        "replacement-report.txt",
    }
)
_INTERNAL_ARTIFACT_NAME_PATTERNS = (
    re.compile(r"^\d{2}_", re.IGNORECASE),  # numbered research copies: 01_page.md, 02_page.html
    re.compile(r"alpha[_-]?input[_-]?goal", re.IGNORECASE),
    re.compile(r"alpha[_-]?full[_-]?goal", re.IGNORECASE),
)
TEXT_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
}
BINARY_MIME_OVERRIDES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def promote_alpha_artifacts(
    *,
    task_id: str,
    paths: WorkspacePaths,
    text_hints: Iterable[str | None] = (),
) -> list[ArtifactManifest]:
    """Promote user-facing Alpha outputs without requiring task-specific glue.

    The task artifact directory is always considered explicit output. Workspace files
    are promoted only when the CLI mentions their absolute path, which avoids sending
    whole project trees while still recovering screenshots, PDFs, zips, and similar
    deliverables that agents created in the workspace.
    """
    candidates: list[Path] = []
    candidates.extend(_iter_files(paths.artifacts))
    candidates.extend(_referenced_files(paths=paths, text="\n".join(item or "" for item in text_hints)))

    manifests: list[ArtifactManifest] = []
    seen_paths: set[Path] = set()
    seen_ids: set[str] = set()
    seen_content_names: set[tuple[str, str]] = set()
    seen_names: set[str] = set()
    for candidate in candidates:
        resolved = _safe_resolve(candidate)
        if resolved is None or resolved in seen_paths:
            continue
        if not _is_allowed_alpha_output(resolved, paths=paths):
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        try:
            size_bytes = resolved.stat().st_size
        except OSError:
            continue
        if size_bytes <= 0 or size_bytes > MAX_PROMOTED_FILE_BYTES:
            continue

        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        name_key = resolved.name.casefold()
        if name_key in seen_names:
            continue
        content_name_key = (digest, resolved.name)
        if content_name_key in seen_content_names:
            continue
        artifact_id = _artifact_id_for(path=resolved, digest=digest)
        if artifact_id in seen_ids:
            continue
        seen_paths.add(resolved)
        seen_ids.add(artifact_id)
        seen_content_names.add(content_name_key)
        seen_names.add(name_key)
        manifests.append(
            ArtifactManifest(
                artifact_id=artifact_id,
                task_id=task_id,
                mime=_infer_mime(resolved),
                sha256=digest,
                path=str(resolved),
                created_by_agent=CREATED_BY,
                kind="output",
                audience=_artifact_audience(resolved),
            )
        )
        if len(manifests) >= MAX_PROMOTED_ARTIFACTS:
            break
    return manifests


def _artifact_audience(path: Path) -> str:
    if _is_internal_alpha_artifact(path):
        return "supporting"
    return "deliverable"


def _is_internal_alpha_artifact(path: Path) -> bool:
    name = path.name
    name_key = name.casefold()
    if name_key in {item.casefold() for item in INTERNAL_ARTIFACT_FILENAMES}:
        return True
    if any(part.casefold() == ".git" for part in path.parts):
        return True
    if name.endswith(".sample"):
        return True
    return any(pattern.search(name) for pattern in _INTERNAL_ARTIFACT_NAME_PATTERNS)


def _iter_files(root: Path) -> list[Path]:
    resolved_root = _safe_resolve(root)
    if resolved_root is None or not resolved_root.exists() or not resolved_root.is_dir():
        return []
    files: list[Path] = []
    for candidate in sorted(resolved_root.rglob("*")):
        if candidate.is_file():
            files.append(candidate)
    return files


def _referenced_files(*, paths: WorkspacePaths, text: str) -> list[Path]:
    if not text.strip():
        return []
    roots = [
        paths.artifacts,
        paths.workspace,
        paths.deployments,
    ]
    matches: list[Path] = []
    for root in roots:
        for root_text in _path_spellings(root):
            if not root_text:
                continue
            pattern = re.compile(re.escape(root_text) + r"[^\s\"'`<>]*")
            for match in pattern.finditer(text):
                raw = match.group(0).rstrip(".,;:)]}")
                if raw:
                    matches.append(Path(raw))
    return matches


def _path_spellings(path: Path) -> list[str]:
    values = [str(path)]
    as_posix = path.as_posix()
    if as_posix not in values:
        values.append(as_posix)
    return values


def _is_allowed_alpha_output(path: Path, *, paths: WorkspacePaths) -> bool:
    if any(part.casefold() == ".git" for part in path.parts):
        return False
    allowed_roots = [
        paths.artifacts,
        paths.workspace,
        paths.deployments,
    ]
    return any(_is_relative_to(path, root) for root in allowed_roots)


def _is_relative_to(path: Path, root: Path) -> bool:
    resolved_root = _safe_resolve(root)
    if resolved_root is None:
        return False
    try:
        path.relative_to(resolved_root)
        return True
    except ValueError:
        return False


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.expanduser().resolve()
    except OSError:
        return None


def _infer_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_MIME_OVERRIDES:
        return TEXT_MIME_OVERRIDES[suffix]
    if suffix in BINARY_MIME_OVERRIDES:
        return BINARY_MIME_OVERRIDES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _artifact_id_for(*, path: Path, digest: str) -> str:
    path_digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
    return f"art_alpha_{digest[:12]}_{path_digest[:8]}"
