-- Adds the 'degraded' document status: the document parsed and indexed, but at
-- least one page failed verification against the PDF's own text layer and was
-- recovered from it. Content is intact; layout and table structure are not.
--
-- Files in this directory only run on a fresh Postgres volume, so apply this to
-- an existing database by hand:
--   docker compose exec -T postgres psql -U postgres -d postgres \
--     < db/migrations/03-parse-quality.sql

ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_status_check;

ALTER TABLE documents ADD CONSTRAINT documents_status_check
    CHECK (status IN ('uploaded','processing','completed','failed','degraded'));
