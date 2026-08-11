-- Lexical half of hybrid retrieval.
--
-- Vector search is weak at exactly the things this corpus is full of: OCR
-- numbers, org numbers, fuse ratings, invoice totals. Those are literal token
-- matches, which is what a text search index does best, so the two retrievers
-- are complementary rather than redundant.
--
-- The 'simple' configuration is deliberate. Stemming configurations have to
-- commit to one language, and this corpus mixes Swedish and English in the
-- same index; 'simple' just lowercases and tokenises, which preserves exact
-- identifiers. Semantic matching across word forms is the vector side's job.
--
-- Generated column rather than a trigger: Postgres maintains it on write, and
-- to_tsvector(regconfig, text) is immutable so it qualifies.
--
-- Files in this directory only run on a fresh Postgres volume, so apply this to
-- an existing database by hand:
--   docker compose exec -T postgres psql -U postgres -d postgres \
--     < db/migrations/04-hybrid-search.sql

ALTER TABLE document_chunks
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED;

CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx
    ON document_chunks USING GIN (content_tsv);
