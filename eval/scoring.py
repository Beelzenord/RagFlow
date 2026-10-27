"""How one answer is judged against its case. Pure, so CI can test it.

A case passes when every check that applies to it passes:

- retrieval   one of the expected documents is among the citations
- include     every `include` pattern matches the answer
- exclude     no `exclude` pattern matches it
- language    the answer is in the case's language
- not_covered for a question the documents do not answer, the answer says so

A check that does not apply is None and counts neither way.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

# The query service's own detector, loaded from its file so the service and the
# evaluation judge language the same way - and eval/ still needs nothing installed.
_spec = importlib.util.spec_from_file_location(
    "query_language", Path(__file__).resolve().parents[1] / "services" / "query" / "app" / "language.py"
)
_language = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_language)


def detect_language(text: str) -> str | None:
    """The answer's language, or None for too little to tell. Answers need a
    little more text than questions before the guess is trusted."""
    return _language.detect_language(text, min_words=4)


# Ways an answer says the documents do not cover something, plus the service's
# own fixed reply when nothing is retrieved at all.
_NOT_COVERED = re.compile(
    r"(?:n't|\bnot\b|\bno\b|couldn'?t|cannot|can'?t|unable|nothing)"
    r"[^.?!]{0,90}?\b(?:cover|mention|say|find|found|information|specif|address|state)"
    # "don't actually include details about a dress code" - but not "does not
    # include equipment", which is an ordinary answer.
    r"|(?:n't|\bnot\b)\s+(?:\w+\s+){0,2}includ\w*\s+(?:\w+\s+){0,2}(?:details?|information|anything)\b"
    r"|looked through the indexed documents"
    r"|\b(?:inte|ingen|inga)\b[^.?!]{0,60}?\b(?:nämn|information|uppgift|täck|framgår|står)"
    r"|\b(?:ikke|ingen)\b[^.?!]{0,60}?\b(?:nevn|nævn|informasjon|information|dekk|dæk|står)"
    # The Scandinavian word order puts the verb first: "nämner inte", "dekker ikke".
    r"|\b(?:nämn|framgår|täck|står|nevn|nævn|dekk|dæk|omtal)\w*\s+(?:\w+\s+){0,2}(?:inte|ikke|ingen|inga)\b",
    re.IGNORECASE,
)


def says_not_covered(answer: str) -> bool:
    return bool(_NOT_COVERED.search(answer or ""))


def _matches(pattern: str, text: str) -> bool:
    return re.search(pattern, text or "", re.IGNORECASE) is not None


def score(case: dict, answer: str, cited: list[str]) -> dict:
    """Every check for one case, each True, False or None (not applicable), and
    the reasons for any that failed."""
    reasons: list[str] = []
    checks: dict[str, bool | None] = {}

    expected = [d.lower() for d in case.get("documents") or []]
    if expected:
        names = {c.lower() for c in cited}
        checks["retrieval"] = any(d in names for d in expected)
        if not checks["retrieval"]:
            reasons.append(f"expected {', '.join(case['documents'])} among the sources")
    else:
        checks["retrieval"] = None

    include = case.get("include") or []
    missing = [p for p in include if not _matches(p, answer)]
    checks["include"] = (not missing) if include else None
    reasons.extend(f"missing /{p}/" for p in missing)

    exclude = case.get("exclude") or []
    present = [p for p in exclude if _matches(p, answer)]
    checks["exclude"] = (not present) if exclude else None
    reasons.extend(f"should not say /{p}/" for p in present)

    lang = case.get("lang")
    if lang:
        found = detect_language(answer)
        # Too short or too mixed to tell is not held against the answer.
        checks["language"] = None if found is None else found == lang
        if checks["language"] is False:
            reasons.append(f"answered in {found}, expected {lang}")
    else:
        checks["language"] = None

    if case.get("not_covered"):
        checks["not_covered"] = says_not_covered(answer)
        if not checks["not_covered"]:
            reasons.append("should say the documents do not cover this")
    else:
        checks["not_covered"] = None

    passed = all(v is not False for v in checks.values())
    return {"passed": passed, "checks": checks, "reasons": reasons}


def summarise(results: list[dict]) -> dict:
    """Pass rates overall and per check, over the cases each check applies to."""
    out: dict[str, dict[str, int]] = {"cases": {"passed": 0, "total": 0}}
    for r in results:
        out["cases"]["total"] += 1
        out["cases"]["passed"] += int(r["passed"])
        for name, value in r["checks"].items():
            if value is None:
                continue
            bucket = out.setdefault(name, {"passed": 0, "total": 0})
            bucket["total"] += 1
            bucket["passed"] += int(value)
    return out
