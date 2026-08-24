"""Adversarial cover for archive inspection and extraction.

An uploaded zip is the most attacker-controlled input Cosmic accepts, so these
tests are organised around what an archive can *do* to us rather than around the
functions under test.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest

from shared.archive_safety import (
    MAX_ENTRIES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    ArchiveRejected,
    inspect_zip,
    normalize_entry_name,
    safe_extract_zip,
)
from shared.bundle_artifacts import (
    is_supported_bundle_artifact,
    is_supported_web_asset_artifact,
)


@pytest.fixture()
def workdir() -> Path:
    root = Path.cwd() / ".codex_test_tmp" / f"archive-{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _write_zip(path: Path, entries: dict[str, bytes | str], *, raw_names: bool = False) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            data = payload.encode("utf-8") if isinstance(payload, str) else payload
            if raw_names:
                info = zipfile.ZipInfo(name)
                archive.writestr(info, data)
            else:
                archive.writestr(name, data)
    return path


# ── Path safety ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "a/../../b",
        "/etc/passwd",
        "//server/share/x",
        "C:/Windows/System32/evil.dll",
        "C:\\Windows\\evil.dll",
        "..\\..\\windows\\system32\\x",
        "with\x00null",
        "bell\x07name",
        "con",
        "COM1.txt",
        "nul/inner.txt",
        "trailingdot./x",
        "trailingspace /x",
        "a/" * 40 + "deep.txt",
        "x" * 300,
    ],
)
def test_unsafe_entry_names_are_refused(name: str) -> None:
    assert normalize_entry_name(name) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("index.html", "index.html"),
        ("./index.html", "index.html"),
        ("assets/app.css", "assets/app.css"),
        ("assets\\app.css", "assets/app.css"),
        ("nested/dir/", "nested/dir/"),
        ("a/./b/c.txt", "a/b/c.txt"),
    ],
)
def test_safe_entry_names_normalize(name: str, expected: str) -> None:
    assert normalize_entry_name(name) == expected


def test_zip_slip_is_refused_at_inspection(workdir: Path) -> None:
    archive = _write_zip(
        workdir / "slip.zip",
        {"index.html": "<html></html>", "../../escape.txt": "pwned"},
        raw_names=True,
    )
    manifest = inspect_zip(archive)
    assert manifest.ok is False
    assert "unsafe path" in (manifest.rejection or "").lower()


def test_zip_slip_never_writes_outside_destination(workdir: Path) -> None:
    archive = _write_zip(
        workdir / "slip.zip", {"../../escape.txt": "pwned"}, raw_names=True
    )
    dest = workdir / "out"
    with pytest.raises(ArchiveRejected):
        safe_extract_zip(archive, dest)
    assert not (workdir.parent / "escape.txt").exists()
    assert not (workdir / "escape.txt").exists()


def test_symlink_entries_are_refused(workdir: Path) -> None:
    archive_path = workdir / "link.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("index.html", "<html></html>")
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    manifest = inspect_zip(archive_path)
    assert manifest.ok is False
    assert "symlink" in (manifest.rejection or "").lower()


def test_special_files_are_refused(workdir: Path) -> None:
    archive_path = workdir / "fifo.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        info = zipfile.ZipInfo("pipe")
        info.external_attr = (stat.S_IFIFO | 0o644) << 16
        archive.writestr(info, "")
    manifest = inspect_zip(archive_path)
    assert manifest.ok is False


# ── Resource exhaustion ──────────────────────────────────────────────────────


def test_compression_bomb_is_refused(workdir: Path) -> None:
    # 40 MB of zeroes compresses to a few KB: ratio far past the ceiling.
    archive = _write_zip(workdir / "bomb.zip", {"bomb.bin": b"\0" * (40 * 1024 * 1024)})
    manifest = inspect_zip(archive)
    assert manifest.ok is False
    assert "zip bomb" in (manifest.rejection or "").lower()


def test_tiny_highly_compressible_file_is_not_mistaken_for_a_bomb(workdir: Path) -> None:
    # The ratio check must not fire on ordinary small text, which compresses well.
    archive = _write_zip(workdir / "small.zip", {"index.html": "<html>" + "a" * 2000 + "</html>"})
    manifest = inspect_zip(archive)
    assert manifest.ok is True


def test_too_many_entries_is_refused(workdir: Path) -> None:
    entries = {f"file_{index}.txt": "x" for index in range(MAX_ENTRIES + 1)}
    archive = _write_zip(workdir / "many.zip", entries)
    manifest = inspect_zip(archive)
    assert manifest.ok is False
    assert "entries" in (manifest.rejection or "").lower()


def test_declared_size_cannot_exceed_the_total_budget(workdir: Path, monkeypatch) -> None:
    from shared import archive_safety

    # Incompressible payloads, so the ratio guard stays out of the way and the
    # total-size ceiling is what does the refusing.
    entries = {f"big_{index}.bin": os.urandom(64 * 1024) for index in range(8)}
    archive = _write_zip(workdir / "huge.zip", entries)
    monkeypatch.setattr(archive_safety, "MAX_TOTAL_UNCOMPRESSED_BYTES", 128 * 1024)
    manifest = inspect_zip(archive)
    assert manifest.ok is False
    assert "expands to more than" in (manifest.rejection or "").lower()


def test_lying_central_directory_is_caught_during_extraction(workdir: Path) -> None:
    """The declared size is metadata; only what the stream produces is trusted."""
    from shared import archive_safety

    archive = _write_zip(workdir / "liar.zip", {"payload.bin": b"\0" * (2 * 1024 * 1024)})
    original = archive_safety.MAX_ENTRY_UNCOMPRESSED_BYTES
    archive_safety.MAX_ENTRY_UNCOMPRESSED_BYTES = 1024
    try:
        with pytest.raises(ArchiveRejected):
            safe_extract_zip(archive, workdir / "out")
    finally:
        archive_safety.MAX_ENTRY_UNCOMPRESSED_BYTES = original


# ── Format confusion ─────────────────────────────────────────────────────────


def test_office_documents_are_not_source_bundles(workdir: Path) -> None:
    archive = _write_zip(workdir / "report.docx", {"word/document.xml": "<w:document/>"})
    manifest = inspect_zip(archive)
    assert manifest.ok is False
    assert "document format" in (manifest.rejection or "").lower()


def test_packaged_executables_are_not_source_bundles(workdir: Path) -> None:
    archive = _write_zip(workdir / "app.jar", {"META-INF/MANIFEST.MF": "Manifest-Version: 1.0"})
    manifest = inspect_zip(archive)
    assert manifest.ok is False
    assert "executable" in (manifest.rejection or "").lower()


def test_non_zip_with_zip_extension_is_refused(workdir: Path) -> None:
    fake = workdir / "notreally.zip"
    fake.write_bytes(b"this is plain text, not an archive")
    manifest = inspect_zip(fake)
    assert manifest.ok is False
    assert "not a valid zip" in (manifest.rejection or "").lower()


def _set_encrypted_flag(archive_path: Path) -> None:
    """Flip the general-purpose "encrypted" bit in every central directory record.

    zipfile cannot *write* encrypted archives, so the bit is set after the fact.
    Readers take this flag at face value, which is exactly what inspect_zip does.
    """
    data = bytearray(archive_path.read_bytes())
    signature = b"PK"
    offset = data.find(signature)
    while offset != -1:
        flags_at = offset + 8
        data[flags_at] |= 0x01
        offset = data.find(signature, offset + 1)
    archive_path.write_bytes(bytes(data))


def test_encrypted_archive_is_refused(workdir: Path) -> None:
    archive_path = _write_zip(workdir / "secret.zip", {"secret.txt": "ciphertext"})
    _set_encrypted_flag(archive_path)
    manifest = inspect_zip(archive_path)
    assert manifest.ok is False
    assert "password" in (manifest.rejection or "").lower()


def test_empty_archive_is_refused(workdir: Path) -> None:
    archive_path = workdir / "empty.zip"
    with zipfile.ZipFile(archive_path, "w"):
        pass
    manifest = inspect_zip(archive_path)
    assert manifest.ok is False
    assert "empty" in (manifest.rejection or "").lower()


def test_directories_only_archive_is_refused(workdir: Path) -> None:
    archive_path = workdir / "dirs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("a/", "")
        archive.writestr("a/b/", "")
    manifest = inspect_zip(archive_path)
    assert manifest.ok is False


# ── Project shape ────────────────────────────────────────────────────────────


def test_static_site_is_recognised_and_wrapper_directory_stripped(workdir: Path) -> None:
    archive = _write_zip(
        workdir / "site.zip",
        {
            "portfolio-main/index.html": "<html><body>hi</body></html>",
            "portfolio-main/assets/app.css": "body{}",
            "portfolio-main/assets/app.js": "console.log(1)",
        },
    )
    manifest = inspect_zip(archive)
    assert manifest.ok is True
    assert manifest.common_root == "portfolio-main"
    assert manifest.project_kind == "static_site"
    assert "index.html" in {item.name for item in manifest.entries}
    assert manifest.file_count == 3


def test_node_project_outranks_a_stray_index_html(workdir: Path) -> None:
    archive = _write_zip(
        workdir / "app.zip",
        {"package.json": "{}", "public/index.html": "<html></html>"},
    )
    manifest = inspect_zip(archive)
    assert manifest.project_kind == "node_project"
    assert "package.json" in manifest.signals


def test_mixed_top_level_keeps_the_wrapper(workdir: Path) -> None:
    archive = _write_zip(
        workdir / "mixed.zip", {"index.html": "<html></html>", "docs/readme.txt": "hi"}
    )
    manifest = inspect_zip(archive)
    assert manifest.common_root is None
    assert manifest.project_kind == "static_site"


def test_platform_cruft_is_filtered(workdir: Path) -> None:
    archive = _write_zip(
        workdir / "mac.zip",
        {
            "site/index.html": "<html></html>",
            "__MACOSX/site/._index.html": "junk",
            "site/.DS_Store": "junk",
        },
    )
    manifest = inspect_zip(archive)
    assert manifest.ok is True
    assert manifest.skipped_cruft == 2
    assert {item.name for item in manifest.entries} == {"index.html"}


def test_manifest_entry_sample_is_capped(workdir: Path) -> None:
    entries = {f"src/file_{index}.js": "x" for index in range(400)}
    archive = _write_zip(workdir / "big.zip", entries)
    manifest = inspect_zip(archive)
    assert manifest.ok is True
    assert manifest.truncated is True
    assert len(manifest.entries) <= 200
    assert manifest.file_count == 400


# ── Extraction ───────────────────────────────────────────────────────────────


def test_extraction_writes_the_expected_tree(workdir: Path) -> None:
    archive = _write_zip(
        workdir / "site.zip",
        {
            "portfolio-main/index.html": "<html>hello</html>",
            "portfolio-main/assets/app.css": "body{}",
        },
    )
    dest = workdir / "out"
    written = safe_extract_zip(archive, dest)
    assert sorted(written) == ["assets/app.css", "index.html"]
    assert (dest / "index.html").read_text(encoding="utf-8") == "<html>hello</html>"
    assert (dest / "assets" / "app.css").is_file()


def test_extraction_strips_the_executable_bit(workdir: Path) -> None:
    archive_path = workdir / "exec.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        info = zipfile.ZipInfo("run.sh")
        info.external_attr = (stat.S_IFREG | 0o777) << 16
        archive.writestr(info, "#!/bin/sh\necho hi\n")
    dest = workdir / "out"
    safe_extract_zip(archive_path, dest)
    mode = (dest / "run.sh").stat().st_mode
    assert not mode & stat.S_IXUSR


def test_nested_archive_is_written_but_not_recursed(workdir: Path) -> None:
    inner = _write_zip(workdir / "inner.zip", {"a.txt": "a"})
    archive = _write_zip(
        workdir / "outer.zip",
        {"index.html": "<html></html>", "vendor/inner.zip": inner.read_bytes()},
    )
    dest = workdir / "out"
    written = safe_extract_zip(archive, dest)
    assert "vendor/inner.zip" in written
    assert (dest / "vendor" / "inner.zip").is_file()
    assert not (dest / "vendor" / "inner").exists()


# ── Classification ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("artifact", "expected"),
    [
        ({"filename": "site.zip"}, True),
        ({"filename": "site.zip", "mime": "application/zip"}, True),
        ({"filename": "report.docx", "mime": "application/zip"}, False),
        ({"filename": "book.epub"}, False),
        ({"filename": "app.jar"}, False),
        ({"filename": "wheel.whl"}, False),
        ({"filename": "notes.pdf"}, False),
        ({"filename": "", "mime": "application/zip"}, True),
    ],
)
def test_bundle_classification(artifact: dict, expected: bool) -> None:
    assert is_supported_bundle_artifact(artifact) is expected


@pytest.mark.parametrize(
    ("artifact", "expected"),
    [
        ({"filename": "index.html"}, True),
        ({"filename": "styles.css"}, True),
        ({"filename": "app.tsx"}, True),
        ({"filename": "", "mime": "text/html"}, True),
        ({"filename": "", "mime": "text/html; charset=utf-8"}, True),
        ({"filename": "data.json"}, False),
        ({"filename": "notes.md"}, False),
        ({"filename": "photo.png"}, False),
    ],
)
def test_web_asset_classification(artifact: dict, expected: bool) -> None:
    assert is_supported_web_asset_artifact(artifact) is expected


# ── End-to-end: upload classification through to a staged workspace ──────────


def _portfolio_zip(path: Path) -> Path:
    return _write_zip(
        path,
        {
            "portfolio-main/index.html": "<html><body>portfolio</body></html>",
            "portfolio-main/assets/site.css": "body{margin:0}",
            "portfolio-main/assets/site.js": "console.log('hi')",
            "portfolio-main/__MACOSX/._index.html": "junk",
        },
    )


def test_gateway_classifies_the_new_kinds_without_stealing_existing_ones() -> None:
    """Order in _supported_artifact_kind is what keeps .docx out of the bundle path."""
    from shared import (
        is_supported_bundle_artifact,
        is_supported_document_artifact,
        is_supported_tabular_artifact,
        is_supported_web_asset_artifact,
    )

    def kind(artifact: dict) -> str | None:
        if is_supported_document_artifact(artifact):
            return "document"
        if is_supported_tabular_artifact(artifact):
            return "spreadsheet"
        if is_supported_bundle_artifact(artifact):
            return "bundle"
        if is_supported_web_asset_artifact(artifact):
            return "web_asset"
        return None

    assert kind({"filename": "report.docx"}) == "document"
    assert kind({"filename": "book.xlsx"}) == "spreadsheet"
    assert kind({"filename": "data.csv"}) == "spreadsheet"
    assert kind({"filename": "paper.pdf"}) == "document"
    assert kind({"filename": "site.zip"}) == "bundle"
    assert kind({"filename": "index.html"}) == "web_asset"
    assert kind({"filename": "mystery.bin"}) is None


def test_handler_registry_flags_the_ambiguous_case() -> None:
    from shared import describe_claims

    bundle = describe_claims({"artifact_id": "a1", "filename": "site.zip", "kind": "bundle"})
    assert [option["handler_id"] for option in bundle["options"]] == ["alpha_project"]
    assert bundle["ambiguous"] is False

    web = describe_claims({"artifact_id": "a2", "filename": "index.html", "kind": "web_asset"})
    assert {option["handler_id"] for option in web["options"]} == {"alpha_project", "read_inline"}
    assert web["ambiguous"] is True

    # Existing kinds must stay entirely outside this mechanism.
    for kind in ("document", "spreadsheet", "image", "map"):
        assert describe_claims({"artifact_id": "x", "filename": "f", "kind": kind}) == {}

    # A kind with no claimant falls through to whatever handles it today.
    assert describe_claims({"artifact_id": "a3", "filename": "x.png", "kind": "image"}) == {}


def test_alpha_unpacks_a_bundle_into_the_workspace(workdir: Path) -> None:
    """The harness receives a tree, never an archive it has to unpack itself."""
    from shared.archive_safety import safe_extract_zip

    archive = _portfolio_zip(workdir / "portfolio.zip")
    staged = workdir / "workspace" / "_cosmic_inputs" / "01_portfolio"
    written = safe_extract_zip(archive, staged)

    assert (staged / "index.html").is_file()
    assert (staged / "assets" / "site.css").is_file()
    # Wrapper directory stripped, mac cruft dropped.
    assert not (staged / "portfolio-main").exists()
    assert not any("__MACOSX" in name for name in written)


def test_orchestrator_note_describes_without_inlining() -> None:
    """The archive's bytes must never reach the conversation."""
    from shared import describe_claims, inspect_zip

    root = Path.cwd() / ".codex_test_tmp" / f"note-{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        archive = _portfolio_zip(root / "portfolio.zip")
        manifest = inspect_zip(archive).as_dict()
        artifact = {
            "artifact_id": "art_1",
            "filename": "portfolio.zip",
            "kind": "bundle",
            "metadata": {"archive": manifest},
        }
        claims = describe_claims(artifact)
        assert claims["options"][0]["intent"] == "alpha.execute"
        assert manifest["project_kind"] == "static_site"
        assert manifest["common_root"] == "portfolio-main"
        assert manifest["file_count"] == 3
        # Nothing in the manifest carries file contents.
        blob = json.dumps(manifest)
        assert "portfolio</body>" not in blob
        assert "console.log" not in blob
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── Plumbing: the manifest has to survive the ingest path ────────────────────


def _manifest_lookup(artifact: dict) -> dict:
    from orchestrator.runtime import OrchestratorRuntime

    return OrchestratorRuntime._artifact_archive_manifest(artifact)


def test_archive_manifest_is_found_however_deeply_ingest_nested_it() -> None:
    """persist_inbound_attachments sweeps unknown keys into a `metadata` bag.

    That bag does not exclude the key `metadata`, so an artifact that round-trips
    through it comes back one level deeper. The lookup must not care.
    """
    archive = {"project_kind": "static_site", "file_count": 3}

    direct = {"metadata": {"archive": archive}}
    nested = {"metadata": {"metadata": {"archive": archive}, "source_channel": "desktop:x"}}
    double = {"metadata": {"metadata": {"metadata": {"archive": archive}}}}

    assert _manifest_lookup(direct) == archive
    assert _manifest_lookup(nested) == archive
    assert _manifest_lookup(double) == archive


def test_archive_manifest_lookup_is_safe_on_junk() -> None:
    assert _manifest_lookup({}) == {}
    assert _manifest_lookup({"metadata": None}) == {}
    assert _manifest_lookup({"metadata": "a string"}) == {}
    assert _manifest_lookup({"metadata": {"archive": "not a dict"}}) == {}
    # Self-referential metadata must terminate rather than spin.
    loop: dict = {}
    loop["metadata"] = loop
    assert _manifest_lookup(loop) == {}


def test_routing_note_describes_a_bundle_without_its_contents() -> None:
    from orchestrator.runtime import OrchestratorRuntime
    from shared import inspect_zip

    root = Path.cwd() / ".codex_test_tmp" / f"note2-{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    try:
        archive = _write_zip(
            root / "portfolio.zip",
            {
                "portfolio-main/index.html": "<html>SECRET_MARKER</html>",
                "portfolio-main/app.js": "console.log('SECRET_MARKER')",
            },
        )
        artifact = {
            "artifact_id": "art_1",
            "filename": "portfolio.zip",
            "kind": "bundle",
            "metadata": {"archive": inspect_zip(archive).as_dict()},
        }
        note = OrchestratorRuntime._describe_routable_artifact(
            OrchestratorRuntime.__new__(OrchestratorRuntime), artifact
        )
        assert note is not None
        text = note["text"]
        assert "portfolio.zip" in text
        assert "static_site" in text
        assert "index.html" not in text or "SECRET_MARKER" not in text
        # The decisive property: no file content reaches the conversation.
        assert "SECRET_MARKER" not in text
        assert "alpha.execute" in text
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_routing_note_flags_ambiguity_for_a_web_asset() -> None:
    from orchestrator.runtime import OrchestratorRuntime

    note = OrchestratorRuntime._describe_routable_artifact(
        OrchestratorRuntime.__new__(OrchestratorRuntime),
        {"artifact_id": "a", "filename": "index.html", "kind": "web_asset"},
    )
    assert note is not None
    assert "ask which" in note["text"].lower()


def test_routing_note_is_absent_for_kinds_nothing_claims() -> None:
    from orchestrator.runtime import OrchestratorRuntime

    # document/spreadsheet included deliberately: they already have a working
    # pipeline, so this feature must be invisible to them.
    for kind in ("image", "map", "document", "spreadsheet", ""):
        note = OrchestratorRuntime._describe_routable_artifact(
            OrchestratorRuntime.__new__(OrchestratorRuntime),
            {"artifact_id": "a", "filename": "x", "kind": kind},
        )
        assert note is None, f"{kind} should keep its existing handling"
