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
