-- Additional transition-scan indexes (docs/binnacle-core/components/02-store-and-migrations.md's
-- "changes feed (indexed transition scans)" promise, ARCHITECTURE.md §4).
-- 0002_indexes.sql covers actor- and time-only transition scans
-- (idx_trans_actor, idx_trans_time) but left two access patterns from
-- adapters.postgres_store unindexed -- discovered by the NFR-7 perf seed test
-- (tests/db/test_perf.py) exceeding its raw p95 target at 10k-decision/
-- 100k-transition scale.
--
-- idx_trans_decision: PostgresStore.history()/transitions_for()'s
-- `WHERE decision_id = %s ORDER BY at ASC, transition_id ASC` -- the
-- single-decision transition read every `history()` call and every
-- tx-scoped lifecycle helper (predecessor_chain's siblings, reactivation's
-- status restore) makes. Previously a full-table sequential scan of
-- `transitions`: a FOREIGN KEY does not implicitly index its own column in
-- Postgres, and no existing index leads with `decision_id`.
--
-- idx_trans_action: PostgresStore.changes()'s `t.action = ANY(%(actions)s)`
-- filter (with or without a `since`/`actor` filter alongside it), ordered
-- `ORDER BY t.at DESC, t.transition_id DESC`. Previously also a full-table
-- scan: idx_trans_time orders by `at` alone with no column to narrow by
-- action first, so an action-filtered changes() query scanned and sorted
-- the whole table. `(action, at DESC)` lets the planner use an index range
-- scan already sorted for that ORDER BY.

CREATE INDEX idx_trans_decision ON {schema}.transitions(decision_id);
CREATE INDEX idx_trans_action   ON {schema}.transitions(action, at DESC);
