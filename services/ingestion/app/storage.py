from __future__ import annotations
import os
from pathlib import Path
from rag_shared.settings import settings


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
