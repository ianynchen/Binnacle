-- Hot-path partial indexes + HNSW vector index (ARCHITECTURE.md §4). Index names
-- are not schema-qualified in CREATE INDEX (Postgres places an index in the same
-- schema as its target table automatically); the target table itself is.

CREATE INDEX idx_dec_active   ON {schema}.decisions(tier, domain, status) WHERE status NOT IN ('archived','discarded');
CREATE INDEX idx_refs_subject ON {schema}.refs(kind, identifier) WHERE role = 'subject';
CREATE INDEX idx_links_to     ON {schema}.links(to_id, kind);
CREATE INDEX idx_trans_time   ON {schema}.transitions(at DESC);
CREATE INDEX idx_trans_actor  ON {schema}.transitions(actor, at DESC);
CREATE INDEX idx_queue_open   ON {schema}.queue(kind, proposed_at) WHERE NOT resolved;
CREATE UNIQUE INDEX idx_queue_dedup ON {schema}.queue(kind, decision_id,
  COALESCE(target_id, '00000000-0000-0000-0000-000000000000'::uuid))
  WHERE NOT resolved;                          -- discovery re-runs cannot duplicate open items
-- pgvector index (HNSW) on embeddings.embedding; cosine distance (nomic-embed
-- text embeddings — OQ-3), filtered joins exclude archived at query time.
CREATE INDEX idx_embeddings_hnsw ON {schema}.embeddings USING hnsw (embedding vector_cosine_ops);
