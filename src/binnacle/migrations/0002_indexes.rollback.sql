-- Rollback for 0002_indexes.sql, exact reverse-of-creation order (see
-- 0001_schema.rollback.sql for why the ordering matters positionally).

DROP INDEX {schema}.idx_embeddings_hnsw;
DROP INDEX {schema}.idx_queue_dedup;
DROP INDEX {schema}.idx_queue_open;
DROP INDEX {schema}.idx_trans_actor;
DROP INDEX {schema}.idx_trans_time;
DROP INDEX {schema}.idx_links_to;
DROP INDEX {schema}.idx_refs_subject;
DROP INDEX {schema}.idx_dec_active;
