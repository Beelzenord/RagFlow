from __future__ import annotations
import logging
import os
import shutil
from pathlib import Path
from rag_shared.settings import settings

log = logging.getLogger(__name__)


def _root() -> Path:
    p = Path(settings.storage_dir)
    (p / "originals").mkdir(parents=True, exist_ok=True)
    (p / "markdown").mkdir(parents=True, exist_ok=True)
    return p


def save_original(document_id: str, filename: str, content: bytes) -> str:
    safe = os.path.basename(filename)
    target = _root() / "originals" / f"{document_id}__{safe}"
    target.write_bytes(content)
    return str(target)


def save_markdown(document_id: str, markdown: str) -> str:
    target = _root() / "markdown" / f"{document_id}.md"
    target.write_text(markdown, encoding="utf-8")
    return str(target)


def read_original(path: str) -> bytes:
    return Path(path).read_bytes()


def evidence_path(document_id: str, chunk_id: str, variant: str) -> Path:
    """Cache location for one rendered evidence image.

    Grouped by document so deleting a document's images is one call, and keyed
    by chunk so re-ingestion - which issues fresh chunk ids - can never serve a
    picture of the old text.
    """
    target = _root() / "evidence" / document_id
    target.mkdir(parents=True, exist_ok=True)
    return target / f"{chunk_id}-{variant}.png"


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temporary sibling so a reader never sees a half-written PNG.

    Two requests for the same tile can render concurrently, and on Azure Files
    a partially flushed file would otherwise be served as a broken image.
    """
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not cache %s: %s", path, exc)
        tmp.unlink(missing_ok=True)


def purge_evidence(document_id: str) -> None:
    """Drop every cached image for a document. Never raises."""
    target = Path(settings.storage_dir) / "evidence" / document_id
    try:
        shutil.rmtree(target, ignore_errors=True)
    except OSError as exc:  # pragma: no cover - rmtree already swallows most
        log.warning("could not purge evidence for %s: %s", document_id, exc)


def resolve_storage_file(path: str) -> Path | None:
    """Return an absolute Path if `path` exists and stays under storage_dir.

    Rejects missing files and any path that escapes the configured root
    (path-traversal / tainted DB row).
    """
    if not path:
        return None
    root = Path(settings.storage_dir).resolve()
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    if not resolved.is_file():
        return None
    return resolved
