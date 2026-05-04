-- Run on first DB init (mounted at /docker-entrypoint-initdb.d).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
