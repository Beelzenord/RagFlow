"""Who a document is for: everyone, or only certain markets.

Pure functions, so the rules are testable without a database. A document with
no rows in document_audience is for everyone (Global); see
db/migrations/05-markets.sql for why that default is safe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

MARKET_CODE = re.compile(r"^[A-Z]{2}$")


class AudienceError(ValueError):
    """A selection the API refuses, with a message fit to show an admin."""


@dataclass(frozen=True)
class Audience:
    countries: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()

    @property
    def everyone(self) -> bool:
        return not self.countries and not self.groups


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def normalize_code(code: str) -> str:
    """Upper-case and check one country code, or raise AudienceError."""
    value = (code or "").strip().upper()
    if not MARKET_CODE.match(value):
        raise AudienceError(f"{code!r} is not a two-letter country code")
    return value


def validate_audience(everyone: bool, countries: Iterable[str], groups: Iterable[str]) -> Audience:
    """Turn a "Who is this for?" answer into the rows to store.

    Everyone wins over any lists sent alongside it, so a form that kept its old
    checkboxes when the admin switched to Everyone still means Everyone. An
    empty "only these markets" is refused rather than stored: with no rows it
    would read back as Global, the opposite of what was asked for.
    """
    if everyone:
        return Audience()
    codes = _dedupe(normalize_code(c) for c in countries if (c or "").strip())
    group_ids: list[str] = []
    for g in groups:
        try:
            group_ids.append(str(UUID(str(g))))
        except ValueError:
            raise AudienceError(f"{g!r} is not a group id") from None
    audience = Audience(codes, _dedupe(group_ids))
    if audience.everyone:
        raise AudienceError("Choose Everyone, or at least one market.")
    return audience


def is_global_sql(doc: str = "d") -> str:
    """SQL that is true when document `doc` is for everyone."""
    return f"NOT EXISTS (SELECT 1 FROM document_audience a WHERE a.document_id = {doc}.id)"


def targets_market_sql(doc: str = "d", param: str = "market") -> str:
    """SQL that is true when document `doc` is limited to markets that include
    the one bound to `param` - named directly, or through a group that
    currently contains it.

    Groups are resolved here, at read time, which is what lets a group edit
    reach every document tagged with it. Retrieval uses this same test, OR'd
    with "is Global"; keep it in step with services/query/app/audience.py so
    the dashboard and the answers agree on what "for Sweden" means.
    """
    return (
        f"EXISTS (SELECT 1 FROM document_audience a"
        f" LEFT JOIN market_group_members m ON m.group_id = a.group_id"
        f" WHERE a.document_id = {doc}.id"
        f" AND (a.market_code = :{param} OR m.market_code = :{param}))"
    )


def applies_to_market_sql(doc: str = "d", param: str = "market") -> str:
    """SQL that is true when document `doc` is among what answers for the market
    bound to `param` draw on: everyone's documents, plus those limited to it.

    The same test retrieval filters with (applies_to_market_sql in
    services/query/app/audience.py), so the dashboard's "Everything Sweden
    sees" is exactly what Sweden's answers can use.
    """
    return f"({is_global_sql(doc)} OR {targets_market_sql(doc, param)})"


def who_filter(who: str | None, include_global: bool = False) -> tuple[str | None, dict[str, str]]:
    """The WHERE clause and params for the dashboard's "who it's for" filter.

    `who` is "global", a country code (directly or through a group) or a group
    id. `include_global` widens a country to everything its answers draw on; it
    means nothing for the others, so it is refused there rather than ignored.
    """
    if not who:
        if include_global:
            raise AudienceError("include_global needs a country code in who")
        return None, {}
    if who == "global":
        if include_global:
            raise AudienceError("include_global needs a country code in who")
        return is_global_sql(), {}
    code = who.upper()
    if MARKET_CODE.match(code):
        sql = applies_to_market_sql(param="who") if include_global else targets_market_sql(param="who")
        return sql, {"who": code}
    try:
        group_id = str(UUID(who))
    except ValueError:
        raise AudienceError("who must be 'global', a country code or a group id") from None
    if include_global:
        raise AudienceError("include_global needs a country code in who")
    return (
        "EXISTS (SELECT 1 FROM document_audience a WHERE a.document_id = d.id AND a.group_id = :who_group)",
        {"who_group": group_id},
    )


# The document list's orders, by the name the dashboard asks for. Each breaks
# ties on the id, so paging never shows a document twice or skips one.
SORTS = {
    "uploaded_desc": "d.created_at DESC, d.id",
    "uploaded_asc": "d.created_at ASC, d.id",
    "name_asc": "lower(d.original_filename) ASC, d.id",
    "name_desc": "lower(d.original_filename) DESC, d.id",
}
DEFAULT_SORT = "uploaded_desc"
