"""Re-ranking: which of the fused candidates reach the answer.

Hybrid retrieval finds the right paragraph more often than it ranks it in the
top six - Sweden's anchor day sat 7th, behind six chunks of the vacation
policy. Here the chat model reads the candidates (rerank_pool of them) and
picks the ones that answer the question, best first.

It can only help: the picks come first, and the fused order fills whatever is
left up to top_k. And it fails open - a slow, broken or empty reply leaves the
fused order exactly as it was.

Pure apart from rerank(), which takes the model client as an argument, so the
parsing and merging are tested without the service's dependencies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Sequence

log = logging.getLogger("query.rerank")

RERANK_SYSTEM = (
    "You rank passages by how useful they are for answering a question. Reply with only a "
    "JSON array of passage numbers, most useful first - for example [3, 1, 7]. Include every "
    "passage that helps answer the question, and leave out the rest. If the answer could "
    "differ between countries, keep the passage for each country."
)

# Enough room for twenty numbers; the reply is never more than that.
RERANK_MAX_TOKENS = 120

# A failing re-ranker would otherwise log on every question.
_WARN_EVERY_SECONDS = 300
_last_warning = 0.0


def build_prompt(question: str, passages: Sequence[tuple[str, str, str | None]], max_chars: int) -> str:
    """Numbered passages as (document name, text, market label or None). The
    label tells the model which country a passage is for, so a question that
    differs by country keeps each one."""
    blocks = []
    for i, (name, text, label) in enumerate(passages, start=1):
        head = f"[{i}] ({name}" + (f" - applies to: {label}" if label else "") + ")"
        body = " ".join((text or "").split())
        if len(body) > max_chars:
            body = body[:max_chars].rsplit(" ", 1)[0] + " ..."
        blocks.append(f"{head}\n{body}")
    return f"Question: {question}\n\nPassages:\n" + "\n\n".join(blocks)


def parse_order(text: str, n: int) -> list[int]:
    """The 0-based passage indices the reply names, in its order. Reads a JSON
    array, or failing that any numbers in the text; ignores anything out of
    range or repeated."""
    numbers: list[Any] = []
    match = re.search(r"\[[^\[\]]*\]", text or "")
    if match:
        try:
            parsed = json.loads(match.group(0))
            numbers = parsed if isinstance(parsed, list) else []
        except ValueError:
            numbers = []
    if not numbers:
        numbers = re.findall(r"\d+", text or "")
    order: list[int] = []
    for value in numbers:
        try:
            i = int(value) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and i not in order:
            order.append(i)
    return order


def merge(order: Sequence[int], n: int, top_k: int) -> list[int]:
    """The picks first, then the fused order fills up to top_k - so a reply
    that names too few, or none, can never lose a passage the fused order had."""
    out = list(order[:top_k])
    for i in range(n):
        if len(out) >= top_k:
            break
        if i not in out:
            out.append(i)
    return out


async def rerank(
    llm: Any,
    question: str,
    rows: Sequence[Any],
    labels: Sequence[str | None],
    top_k: int,
    *,
    timeout: float,
    max_chars: int,
) -> list[Any]:
    """The rows to answer from: re-ranked, or the fused order's first top_k
    when the model cannot be used."""
    global _last_warning
    if len(rows) <= top_k:
        return list(rows)
    passages = [(r["original_filename"], r["content"], label) for r, label in zip(rows, labels)]
    try:
        reply = await asyncio.wait_for(
            llm.complete(
                RERANK_SYSTEM,
                build_prompt(question, passages, max_chars),
                max_tokens=RERANK_MAX_TOKENS,
                temperature=0,
                timeout=timeout,
            ),
            timeout=timeout + 1,
        )
    except Exception as exc:  # noqa: BLE001 - any failure means "keep the fused order"
        now = time.monotonic()
        if now - _last_warning > _WARN_EVERY_SECONDS:
            _last_warning = now
            log.warning("re-ranking failed, keeping the fused order: %s", exc or type(exc).__name__)
        return list(rows[:top_k])
    order = parse_order(reply, len(rows))
    return [rows[i] for i in merge(order, len(rows), top_k)]
