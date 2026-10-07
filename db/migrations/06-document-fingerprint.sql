-- A fingerprint of each uploaded file, so the same file is not indexed twice.
--
-- Uploading identical bytes a second time used to produce a second document:
-- a second LlamaParse bill, a second set of embeddings, and two copies of every
-- chunk competing for the same retrieval slots. With a hash on the row, /ingest
-- can recognise the file and answer 409 with the document that already holds it.
--
-- Not unique on purpose. A deliberate duplicate - the same template uploaded
-- under two collections, say - is legitimate and goes through with
-- allow_duplicate. The index exists to make the lookup cheap, not to forbid it.
--
-- Existing rows start with NULL and are filled by
-- POST /documents/backfill-fingerprints, which hashes the stored originals.
--
-- Files in this directory only run on a fresh Postgres volume, so apply this to
-- an existing database by hand:
--   docker compose exec -T postgres psql -U postgres -d postgres \
--     < db/migrations/06-document-fingerprint.sql

ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_sha256 TEXT;

CREATE INDEX IF NOT EXISTS documents_content_sha256_idx
    ON documents (content_sha256);
