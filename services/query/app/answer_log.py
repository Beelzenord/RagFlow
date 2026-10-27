"""The question log: what people ask, whether the documents answered it, and
what they thought of the answer - the Dashboard's Insights.

Anonymous by design: nothing identifies who asked. Rows are deleted after
ANSWER_LOG_DAYS (see db/migrations/06-answer-log.sql).

Pure helpers and SQL only; main.py runs them. Grouping and the summary are
plain functions so they are tested without a database.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Any, Iterable, Mapping

# --- Did the answer answer? ---------------------------------------------------------
# A cheap check after each answer, so a question the documents do not cover is
# counted even when nobody presses a thumb.

# "Judge by what it gives": answers open by saying what the documents do not
# cover, and that opening alone used to make a useful answer NOT_COVERED.
COVERAGE_SYSTEM = (
    "You check whether an assistant's answer gave the person what they asked for, from "
    "their documents. Judge by what the answer gives, not by how it opens: an answer that "
    "first says the documents are not specific, then gives the rule that answers the "
    "question, has answered it. Reply with exactly one word: ANSWERED if it gives what was "
    "asked, PARTIAL if it gives only part of what was asked, or NOT_COVERED if nothing it "
    "gives answers the question."
)
COVERAGE_MAX_TOKENS = 4
COVERAGE_TIMEOUT_S = 8.0

COVERAGES = ("answered", "partial", "not_covered", "no_hits")
# Worst first: what an admin should look at.
_SEVERITY = {"no_hits": 0, "not_covered": 1, "partial": 2, "answered": 3, None: 4}
NEEDS_ATTENTION = {"no_hits", "not_covered", "partial"}


def coverage_prompt(question: str, answer: str) -> str:
    return f"Question: {question}\n\nAnswer: {answer[:3000]}"


def parse_coverage(text: str) -> str | None:
    # Letters only, so "Not covered.", "**NOT_COVERED**" and " not_covered" agree.
    word = re.sub(r"[^A-Z]", "", (text or "").upper())
    if word.startswith("NOTCOVERED"):
        return "not_covered"
    if word.startswith("PARTIAL"):
        return "partial"
    if word.startswith("ANSWERED"):
        return "answered"
    return None


# --- Storing ------------------------------------------------------------------------

ANSWER_MAX_CHARS = 4000
COMMENT_MAX_CHARS = 500

INSERT_SQL = (
    "INSERT INTO answer_log (id, question, search_query, lang, market, document_id, voice, "
    "answer, sources, coverage) VALUES (:id, :question, :search_query, :lang, :market, "
    "CAST(:document_id AS uuid), :voice, :answer, CAST(:sources AS jsonb), :coverage)"
)
COVERAGE_SQL = "UPDATE answer_log SET coverage = :coverage WHERE id = :id AND coverage IS NULL"
FEEDBACK_SQL = (
    "UPDATE answer_log SET rating = :rating, comment = :comment, rated_at = now() WHERE id = :id"
)
PURGE_SQL = "DELETE FROM answer_log WHERE created_at < now() - make_interval(days => :days)"
INSIGHTS_SQL = (
    "SELECT id::text AS id, created_at, question, lang, market, voice, answer, sources, "
    "coverage, rating, comment FROM answer_log "
    "WHERE created_at >= now() - make_interval(days => :days) {market} "
    "ORDER BY created_at DESC LIMIT :cap"
)
# Enough history for 90 days of a busy office, without unbounded reads.
INSIGHTS_ROW_CAP = 5000


def sources_of(citations: Iterable[Any]) -> list[dict[str, str]]:
    """One entry per cited document, in citation order."""
    seen: dict[str, dict[str, str]] = {}
    for c in citations:
        doc_id = str(getattr(c, "document_id", None) or c["document_id"])
        if doc_id not in seen:
            name = getattr(c, "filename", None) or c["filename"]
            seen[doc_id] = {"document_id": doc_id, "filename": name}
    return list(seen.values())


def normalize_question(text: str) -> str:
    """The same question asked twice, written slightly differently, groups
    together: case, spacing and trailing punctuation do not count."""
    t = " ".join((text or "").lower().split())
    return t.strip(" ?!.,;:\"'”“’")


# --- Reading: the Insights view ----------------------------------------------------------

def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def summarize(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter()
    for r in rows:
        counts["total"] += 1
        counts[r.get("coverage") or "pending"] += 1
        if r.get("rating") == 1:
            counts["up"] += 1
        elif r.get("rating") == -1:
            counts["down"] += 1
    keys = ("total", "answered", "partial", "not_covered", "no_hits", "pending", "up", "down")
    return {k: counts.get(k, 0) for k in keys}


def group_questions(rows: Iterable[Mapping[str, Any]], attention_only: bool = True) -> list[dict[str, Any]]:
    """Questions grouped by their normalised wording, newest rows first in, and
    sorted so what most needs an admin comes first: the most thumbs-down and
    unanswered askings, then the most asked."""
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = normalize_question(r["question"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "question": r["question"],
                "count": 0,
                "last_asked": _iso(r.get("created_at")),
                "markets": set(),
                "coverage": Counter(),
                "worst": None,
                "up": 0,
                "down": 0,
                "comments": [],
                "answer": r.get("answer") or "",
                "lang": r.get("lang"),
                "_sources": Counter(),
            }
        g["count"] += 1
        g["markets"].add(r.get("market") or "")
        cov = r.get("coverage")
        g["coverage"][cov or "pending"] += 1
        if _SEVERITY.get(cov, 4) < _SEVERITY.get(g["worst"], 4):
            g["worst"] = cov
        if r.get("rating") == 1:
            g["up"] += 1
        elif r.get("rating") == -1:
            g["down"] += 1
        if r.get("comment") and len(g["comments"]) < 5:
            g["comments"].append({"text": r["comment"], "at": _iso(r.get("created_at"))})
        for s in r.get("sources") or []:
            g["_sources"][s.get("filename") or ""] += 1

    out = []
    for g in groups.values():
        unanswered = sum(g["coverage"][c] for c in NEEDS_ATTENTION)
        g["needs_attention"] = bool(unanswered or g["down"])
        if attention_only and not g["needs_attention"]:
            continue
        g["unanswered"] = unanswered
        g["markets"] = sorted(g["markets"])
        g["coverage"] = dict(g["coverage"])
        g["sources"] = [name for name, _ in g.pop("_sources").most_common(3) if name]
        out.append(g)
    # Two stable sorts: most recently asked first among equals, then what most
    # needs attention, then the most asked.
    out.sort(key=lambda g: g["last_asked"], reverse=True)
    out.sort(key=lambda g: (-(g["down"] + g["unanswered"]), -g["count"]))
    return out
