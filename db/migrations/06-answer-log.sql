-- What people ask, whether the documents answered it, and what they thought.
--
-- One row per answer. Admins see these on the Dashboard's Insights view: which
-- questions the documents do not cover (per market), and which answers people
-- marked as unhelpful - what is missing, and where.
--
-- Anonymous by design: nothing here identifies who asked. Questions can still
-- contain personal details ("my child is ill..."), so rows are deleted after
-- ANSWER_LOG_DAYS (90 by default) by the query service, and the log can be
-- turned off with ANSWER_LOG_ENABLED=false.
--
-- Files in this directory only run on a fresh Postgres volume, so apply this to
-- an existing database by hand:
--   docker compose exec -T postgres psql -U postgres -d postgres \
--     < db/migrations/06-answer-log.sql

CREATE TABLE IF NOT EXISTS answer_log (
    id            UUID PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The person's own wording, and the standalone question retrieval used
    -- when a follow-up was rewritten.
    question      TEXT        NOT NULL,
    search_query  TEXT,
    lang          TEXT,
    market        TEXT,
    -- Set when the question was aimed at one document ("Search in").
    document_id   UUID,
    voice         BOOLEAN     NOT NULL DEFAULT false,
    answer        TEXT        NOT NULL,
    -- [{"document_id": ..., "filename": ...}], one entry per cited document.
    sources       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- answered / partial / not_covered, from a quick check after the answer;
    -- no_hits when nothing at all was retrieved; NULL until checked.
    coverage      TEXT CHECK (coverage IN ('answered', 'partial', 'not_covered', 'no_hits')),
    rating        SMALLINT CHECK (rating IN (-1, 1)),
    comment       TEXT CHECK (char_length(comment) <= 500),
    rated_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS answer_log_created_at_idx ON answer_log (created_at DESC);
CREATE INDEX IF NOT EXISTS answer_log_needs_attention_idx
    ON answer_log (created_at DESC)
    WHERE coverage IN ('partial', 'not_covered', 'no_hits') OR rating = -1;
