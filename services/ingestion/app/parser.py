"""Wraps LlamaParse. Pipes raw bytes in, gets markdown out (with page markers
when LlamaParse can derive them — true for PDFs, not for single JPEGs)."""
from __future__ import annotations
import asyncio
from pathlib import Path
from llama_parse import LlamaParse
from rag_shared.settings import settings


def _client() -> LlamaParse:
    if not settings.llama_cloud_api_key:
        raise RuntimeError("LLAMA_CLOUD_API_KEY is not set")
    return LlamaParse(
        api_key=settings.llama_cloud_api_key,
        result_type="markdown",
        # 'auto' lets LlamaParse pick OCR mode for image-only PDFs / JPEGs.
        parsing_instruction=(
            "Extract all text faithfully as clean Markdown. Preserve headings, "
            "tables (as Markdown tables), and lists. Do not summarize."
        ),
    )


async def parse_to_markdown(file_path: str, file_type: str) -> str:
    """Returns Markdown. Inserts <!-- page: N --> markers between pages when
    LlamaParse returns per-page documents (PDFs)."""
    parser = _client()
    # llama_parse exposes a sync `load_data` that does its own HTTP — run in a
    # thread so we don't block the event loop.
    docs = await asyncio.to_thread(parser.load_data, file_path)

    if not docs:
        return ""
    if len(docs) == 1:
        return docs[0].text or ""

    parts: list[str] = []
    for i, d in enumerate(docs, start=1):
        parts.append(f"<!-- page: {i} -->\n\n{(d.text or '').strip()}")
    return "\n\n".join(parts)
