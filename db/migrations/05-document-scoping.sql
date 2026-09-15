-- Regional scoping for documents.
--
-- A global firm's policies differ by country: a German employee asking about
-- vacation should get the German rules, not the Swedish ones. See
-- docs/document-scoping.md for the reasoning behind the shape below.
--
-- This migration is inert on its own. Every existing row lands with
-- applies_to_regions = NULL, which reads as "applies everywhere", so nothing
-- changes for anyone until the query filter is wired up in a later pass. That
-- is deliberate: tagging should be possible, and checkable, before a tagging
-- mistake can produce a wrong answer for an employee.
--
-- Two choices worth knowing about:
--
--   * Global is the *absence* of a constraint, not a magic value in the array.
--     "GLOBAL" cannot then be misspelled into a new region, and the filter is
--     one clause: applies_to_regions IS NULL OR applies_to_regions && ARRAY[...].
--
--   * Groups are expanded to leaf regions when a document is tagged, not when it
--     is queried, which keeps the read path a plain array overlap. Same
--     principle the rest of this system is built on: expensive work at
--     ingestion, cheap reads at query time. The cost is re-expanding when group
--     membership changes, which is rare and is an admin action.
--
-- Files in this directory only run on a fresh Postgres volume, so apply this to
-- an existing database by hand:
--   docker compose exec -T postgres psql -U postgres -d postgres \
--     < db/migrations/05-document-scoping.sql

-- Leaf regions. ISO 3166-1 alpha-2 so the codes are not ours to argue about.
CREATE TABLE IF NOT EXISTS regions (
    code  TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

-- Named sets, so nobody ticks 27 boxes for an EU directive. Groups may overlap
-- freely - Germany is in both EU and DACH - because precedence is resolved by
-- the specificity of the entry that matched, not by the group itself.
CREATE TABLE IF NOT EXISTS region_groups (
    code           TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    member_regions TEXT[] NOT NULL DEFAULT '{}',
    -- 0 = everywhere, 1 = multi-country group. Leaf regions are 2, which the
    -- expansion code applies as a constant rather than storing per row.
    specificity    INTEGER NOT NULL DEFAULT 1
);

ALTER TABLE documents
    -- Expanded leaves. NULL means global; see the header.
    ADD COLUMN IF NOT EXISTS applies_to_regions TEXT[],
    -- What the admin actually picked, with the specificity of each entry:
    --   [{"code":"EU","kind":"group","specificity":1},
    --    {"code":"CH","kind":"region","specificity":2}]
    -- Kept rather than collapsed to one number, because specificity is a
    -- property of the match: a German matches the row above through EU, a Swiss
    -- employee through CH.
    ADD COLUMN IF NOT EXISTS scope_entries JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Human labels, denormalised for display and citations.
    ADD COLUMN IF NOT EXISTS scope_labels TEXT[],
    -- Dimensions beyond region - department, employment type - when they arrive.
    -- Generic on purpose: their shape is not known yet and they are not on the
    -- hot query path, so adding one should not cost a migration.
    ADD COLUMN IF NOT EXISTS scope JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- The confidentiality hook. Unused: this system scopes for relevance, not
    -- secrecy. It exists so that the first genuinely restricted document is a
    -- flag rather than a migration and a full reprocess.
    ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'normal';

ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_sensitivity_check;
ALTER TABLE documents ADD CONSTRAINT documents_sensitivity_check
    CHECK (sensitivity IN ('normal','restricted'));

-- && (overlap) is the operator the filter will use, and GIN serves it.
CREATE INDEX IF NOT EXISTS documents_applies_to_regions_idx
    ON documents USING GIN (applies_to_regions);
CREATE INDEX IF NOT EXISTS documents_scope_idx
    ON documents USING GIN (scope);

-- Starter vocabulary. Expected to be edited: add the markets you actually
-- operate in and delete the rest. The expansion code refuses any code that is
-- not in these tables, so a typo cannot become a silently-global document.
INSERT INTO regions (code, label) VALUES
    ('AT','Austria'), ('BE','Belgium'), ('BG','Bulgaria'), ('HR','Croatia'),
    ('CY','Cyprus'), ('CZ','Czechia'), ('DK','Denmark'), ('EE','Estonia'),
    ('FI','Finland'), ('FR','France'), ('DE','Germany'), ('GR','Greece'),
    ('HU','Hungary'), ('IE','Ireland'), ('IT','Italy'), ('LV','Latvia'),
    ('LT','Lithuania'), ('LU','Luxembourg'), ('MT','Malta'),
    ('NL','Netherlands'), ('PL','Poland'), ('PT','Portugal'),
    ('RO','Romania'), ('SK','Slovakia'), ('SI','Slovenia'), ('ES','Spain'),
    ('SE','Sweden'), ('NO','Norway'), ('IS','Iceland'), ('CH','Switzerland'),
    ('GB','United Kingdom'), ('US','United States')
ON CONFLICT (code) DO NOTHING;

INSERT INTO region_groups (code, label, member_regions, specificity) VALUES
    ('GLOBAL', 'Everywhere', '{}', 0),
    ('EU', 'European Union',
     '{AT,BE,BG,HR,CY,CZ,DK,EE,FI,FR,DE,GR,HU,IE,IT,LV,LT,LU,MT,NL,PL,PT,RO,SK,SI,ES,SE}', 1),
    ('NORDICS', 'Nordics', '{SE,NO,DK,FI,IS}', 1),
    ('DACH', 'DACH', '{DE,AT,CH}', 1)
ON CONFLICT (code) DO NOTHING;
