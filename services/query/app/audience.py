"""Restrict retrieval to the documents meant for one market.

A document with no rows in document_audience is for everyone (Global), so it
matches every market. Otherwise it matches when a row names the market directly
or names a group that currently contains it - groups are resolved here, at read
time, so editing a group changes what it covers immediately.

The "limited to this market" half is the same test the dashboard's "Who" filter
uses; keep it in step with services/ingestion/app/audience.py so the dashboard
and the answers agree on what "for Sweden" means.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


def applies_to_market_sql(doc: str = "d", param: str = "market") -> str:
    return (
        f"(NOT EXISTS (SELECT 1 FROM document_audience a WHERE a.document_id = {doc}.id)"
        f" OR EXISTS (SELECT 1 FROM document_audience a"
        f" LEFT JOIN market_group_members m ON m.group_id = a.group_id"
        f" WHERE a.document_id = {doc}.id"
        f" AND (a.market_code = :{param} OR m.market_code = :{param})))"
    )


def market_filter(market: str | None, document_id: str | None) -> tuple[str | None, dict[str, str]]:
    """The WHERE clause and params for "answers for this market", or None.

    Asking about one named document overrides the market: whoever picked that
    document wants it searched, and would otherwise get no answer at all
    whenever it is not meant for the market they have selected.
    """
    if not market or document_id:
        return None, {}
    return applies_to_market_sql(), {"market": market}


# --- Who each retrieved source is for -------------------------------------------
# Retrieval above decides which documents may answer; this tells the model, and
# the reader, who each of them applies to - so an answer drawn from Norway's
# policy says it is Norway's rule instead of reading as everyone's.

# Every audience row of the given documents, with the country's name, or the
# group's name and its members' names. Global documents have no rows.
AUDIENCE_LABELS_SQL = (
    "SELECT a.document_id::text AS document_id, m.code AS code, m.name AS country, "
    "       g.name AS group_name, "
    "       ARRAY(SELECT mm.name FROM market_group_members gm "
    "             JOIN markets mm ON mm.code = gm.market_code "
    "             WHERE gm.group_id = g.id ORDER BY mm.name) AS members "
    "FROM document_audience a "
    "LEFT JOIN markets m ON m.code = a.market_code "
    "LEFT JOIN market_groups g ON g.id = a.group_id "
    "WHERE a.document_id = ANY(CAST(:ids AS uuid[]))"
)

MARKET_NAME_SQL = "SELECT name FROM markets WHERE code = :code"


def _clean(value: object) -> str:
    # Admin-typed names reach the prompt; one line each, no stray whitespace.
    return " ".join(str(value or "").split())


def audience_by_document(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, list]]:
    """Group AUDIENCE_LABELS_SQL rows by document, sorted so prompts are stable."""
    out: dict[str, dict[str, list]] = {}
    for row in rows:
        entry = out.setdefault(str(row["document_id"]), {"countries": [], "groups": []})
        if row.get("code"):
            entry["countries"].append({"code": row["code"], "name": _clean(row.get("country")) or row["code"]})
        elif row.get("group_name"):
            members = sorted(_clean(m) for m in (row.get("members") or []) if _clean(m))
            entry["groups"].append({"name": _clean(row["group_name"]), "members": members})
    for entry in out.values():
        entry["countries"].sort(key=lambda c: c["name"].lower())
        entry["groups"].sort(key=lambda g: g["name"].lower())
    return out


def prompt_label(audience: Mapping[str, list] | None) -> str | None:
    """Who a source applies to, for the model: "Scandinavia (Denmark, Norway,
    Sweden), Germany". Groups carry their members, because the model cannot
    otherwise know that Scandinavia covers someone in Sweden. None is Global."""
    if not audience:
        return None
    parts = [
        f"{g['name']} ({', '.join(g['members'])})" if g["members"] else g["name"]
        for g in audience.get("groups", [])
    ]
    parts += [c["name"] for c in audience.get("countries", [])]
    return ", ".join(parts) or None


def citation_audience(audience: Mapping[str, list] | None) -> dict[str, list[str]] | None:
    """Who a source applies to, for the browser: country codes, which the page
    names in the reader's language, and group names. Never group members - what
    a group is made of is admin knowledge, kept from readers as /api/markets
    does."""
    if not audience or not (audience.get("countries") or audience.get("groups")):
        return None
    return {
        "countries": [c["code"] for c in audience.get("countries", [])],
        "groups": [g["name"] for g in audience.get("groups", [])],
    }


def market_guidance(chosen_market: str | None) -> str:
    """How to read the "applies to" marks. Only added when at least one source is
    limited to certain markets, so a corpus without markets sees no change.

    A source for a specific market beats one for everyone in that market: tagging
    a document for one country only means something if it does."""
    lines = [
        'Each source above is marked "applies to: everyone" or with the markets '
        "(countries) it is limited to.",
        "- A source limited to certain markets applies only there. Never present its rule "
        "as the rule for everyone - not even when it is the only source that answers.",
        "- Where a source for everyone and a source for a specific market disagree, the "
        "market-specific source applies in that market; the source for everyone covers "
        "the rest.",
    ]
    if chosen_market:
        lines.append(f"- The person asking is in {chosen_market}. Answer for {chosen_market}.")
    else:
        lines.append(
            "- The person asking has not chosen a market. If the answer differs between "
            "markets, give each market's own rule, naming the country - for example "
            '"In Sweden ..., in Norway ...". Mention a rule for everyone only if a source '
            "for everyone actually gives one."
        )
    lines.append(
        "- If the sources only answer for some markets, say which markets your answer is "
        "for. Do not guess what applies anywhere else."
    )
    # Seen in testing: this block is English, and without the line below a
    # question asked in Swedish came back in English - only when the block was
    # present. Answers must keep following the language of the question.
    lines.append(
        "- Write your answer in the same language as the question, whatever language these "
        "notes and the sources are in."
    )
    return "\n".join(lines)
