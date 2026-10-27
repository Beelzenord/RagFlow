"""Which language a question is in, and the rule that keeps the answer in it.

Pure and standard-library only: eval/scoring.py loads this file directly, so
the service and the answer evaluation judge language the same way.

Seen in the first evaluation run: an English question about a Swedish rule, or
about a document written in Swedish, came back in Swedish - the sources and
"the person asking is in Sweden" outweighed a language rule buried among the
market notes. Naming the question's language in the system prompt fixes that.
"""
from __future__ import annotations

import re
from collections import Counter

# The interface's languages, named in English for the prompt.
LANG_NAMES: dict[str, str] = {
    "en": "English",
    "sv": "Swedish",
    "nb": "Norwegian (Bokmål)",
    "fi": "Finnish",
    "da": "Danish",
}

# Words that are common in one of the five languages and rare in the others.
# Swedish, Norwegian and Danish share a lot, so each list leans on what sets it
# apart: "och/är/inte/jag" (sv), "ikke/hva/deg/av" (nb), "ikke/hvad/dig/af" (da).
_MARKERS: dict[str, set[str]] = {
    "en": {"the", "and", "is", "are", "you", "your", "to", "of", "for", "with", "not", "can",
           "be", "on", "this", "that", "from", "if", "it", "or", "will", "have", "do", "does",
           "my", "what", "how", "when", "who", "which", "should", "get", "am"},
    "sv": {"och", "är", "inte", "att", "du", "för", "med", "som", "på", "av", "till", "har",
           "kan", "om", "får", "dig", "din", "ditt", "vad", "också", "eller", "jag", "när",
           "måste", "efter", "bara", "från", "dagar", "ska", "hur", "vilka", "mitt", "min"},
    "nb": {"og", "er", "ikke", "å", "du", "for", "med", "som", "på", "av", "til", "har",
           "kan", "om", "får", "deg", "din", "hva", "også", "eller", "jeg", "når", "må",
           "etter", "bare", "fra", "dager", "skal", "hvis", "hvordan", "mitt", "min"},
    "da": {"og", "er", "ikke", "at", "du", "for", "med", "som", "på", "af", "til", "har",
           "kan", "om", "får", "dig", "din", "hvad", "også", "eller", "jeg", "når", "skal",
           "efter", "kun", "fra", "dage", "hvis", "hvordan", "mit", "min"},
    "fi": {"ja", "on", "ei", "että", "se", "voit", "sinun", "kun", "tai", "mutta", "jos",
           "myös", "vain", "ovat", "mitä", "kanssa", "voi", "olet", "sinä", "jotka", "tämä",
           "minun", "voinko", "saanko", "paljonko", "miten", "milloin", "kuinka"},
}

_WORD = re.compile(r"[a-zåäöøæéü]+", re.IGNORECASE)


def detect_language(text: str, min_words: int = 3) -> str | None:
    """The best-scoring of en, sv, nb, da, fi - or None when there is too
    little to tell, or a tie."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    if len(words) < min_words:
        return None
    counts = Counter(words)
    scores = {lang: sum(counts[w] for w in markers) for lang, markers in _MARKERS.items()}
    # Letters only one family uses settle many ties between the Nordic languages.
    lowered = (text or "").lower()
    if "ø" in lowered or "æ" in lowered:
        scores["sv"] -= 3
        scores["fi"] -= 3
    if "ä" in lowered or "ö" in lowered:
        scores["nb"] -= 3
        scores["da"] -= 3
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top <= 0 or top == second:
        return None
    return best


def answer_language_rule(question: str, lang: str | None = None) -> str:
    """The sentence the system prompt ends with. `lang` - set by voice, which
    knows what it heard - wins; otherwise the question's own language is named
    when it can be told, and described when it cannot."""
    name = LANG_NAMES.get((lang or "").lower()) or LANG_NAMES.get(detect_language(question) or "")
    if lang and LANG_NAMES.get(lang.lower()):
        return f"Answer in {name}."
    if name:
        return (
            f"Answer in {name}, the language of the question - not the language of the "
            "sources, nor that of the country they apply to."
        )
    return (
        "Answer in the language the question is written in - not the language of the "
        "sources, nor that of the country they apply to."
    )
