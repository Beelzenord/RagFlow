-- Markets, and who each document is for.
--
-- An admin adds the ISO countries the firm operates in (markets), may name
-- groups of them (e.g. Scandinavia = SE, NO, DK), and says per document whether
-- it is for everyone or only for certain markets. A person chatting picks their
-- market, and retrieval then draws on documents for everyone plus documents for
-- that market. This steers relevance; it is not access control - anyone can
-- switch markets.
--
-- A document with no audience rows is for everyone (Global). That is the
-- default, so every document uploaded before this existed is Global without a
-- data migration. It is safe only because nothing can quietly remove the last
-- row: the foreign keys below refuse to delete a country or group a document
-- still uses, and the API refuses an empty "only these markets" selection.
--
-- Groups are stored by reference, never expanded into the document. Editing a
-- group's members therefore changes, immediately, every document tagged with it.
--
-- Files in this directory only run on a fresh Postgres volume, so apply this to
-- an existing database by hand:
--   docker compose exec -T postgres psql -U postgres -d postgres \
--     < db/migrations/05-markets.sql

CREATE TABLE IF NOT EXISTS markets (
    code       TEXT PRIMARY KEY CHECK (code ~ '^[A-Z]{2}$'),
    -- Copied from the ISO catalogue in the ingestion service when the country
    -- is added, so a reader's picker never needs that catalogue.
    name       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market_groups (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT        NOT NULL CHECK (btrim(name) <> ''),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Scandinavia" and "scandinavia" would be two names for one thing in a picker.
CREATE UNIQUE INDEX IF NOT EXISTS market_groups_name_key ON market_groups (lower(name));

DROP TRIGGER IF EXISTS market_groups_updated_at ON market_groups;
CREATE TRIGGER market_groups_updated_at
    BEFORE UPDATE ON market_groups
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE IF NOT EXISTS market_group_members (
    group_id    UUID NOT NULL REFERENCES market_groups(id) ON DELETE CASCADE,
    -- No ON DELETE: a country cannot be removed while a group still contains it.
    market_code TEXT NOT NULL REFERENCES markets(code),
    PRIMARY KEY (group_id, market_code)
);

CREATE INDEX IF NOT EXISTS market_group_members_code_idx ON market_group_members (market_code);

CREATE TABLE IF NOT EXISTS document_audience (
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    -- Exactly one of these per row. Neither has ON DELETE, so a country or group
    -- a document uses cannot be deleted out from under it - which would
    -- otherwise turn the document Global without anyone deciding that.
    market_code TEXT REFERENCES markets(code),
    group_id    UUID REFERENCES market_groups(id),
    CHECK (num_nonnulls(market_code, group_id) = 1)
);

CREATE UNIQUE INDEX IF NOT EXISTS document_audience_market_key
    ON document_audience (document_id, market_code) WHERE market_code IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS document_audience_group_key
    ON document_audience (document_id, group_id) WHERE group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS document_audience_code_idx  ON document_audience (market_code);
CREATE INDEX IF NOT EXISTS document_audience_group_idx ON document_audience (group_id);
