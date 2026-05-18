"""Local cross-encoder reranker.

Sits between the pgvector ANN call and the LLM: we fetch a larger pool of
candidates from the vector index, then this module re-scores `(question, chunk)`
pairs with `BAAI/bge-reranker-v2-m3` (or whichever model is configured) and
keeps the top-N. Cross-encoders are much stronger than dot-product on
relevance, at the cost of a few hundred ms per query on CPU.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from rag_shared.settings import settings

log = logging.getLogger(__name__)


class Reranker:
    def __init__(self, model_name: str | None = None) -> None:
        # Import lazily so the query service can still boot in environments
        # where sentence-transformers / torch aren't installed (dev, tests).
        from sentence_transformers import CrossEncoder

        name = model_name or settings.reranker_model
        log.info("loading reranker model %s", name)
        self.model = CrossEncoder(name, max_length=512)

    def rerank(
        self,
        query: str,
        rows: Sequence[dict[str, Any]],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Return the top_n rows by cross-encoder score, preserving row shape."""
        if not rows:
            return []
        pairs = [(query, r["content"]) for r in rows]
        scores = self.model.predict(
            pairs, batch_size=32, show_progress_bar=False
        )
        ranked = sorted(
            zip(rows, scores, strict=True),
            key=lambda x: float(x[1]),
            reverse=True,
        )
        return [r for r, _ in ranked[:top_n]]
