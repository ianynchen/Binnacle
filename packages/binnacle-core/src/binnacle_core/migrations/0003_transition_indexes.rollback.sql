-- Rollback for 0003_transition_indexes.sql, exact reverse-of-creation order
-- (see 0001_schema.rollback.sql for why the ordering matters positionally).

DROP INDEX {schema}.idx_trans_action;
DROP INDEX {schema}.idx_trans_decision;
