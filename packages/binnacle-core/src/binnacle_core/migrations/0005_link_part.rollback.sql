-- Rollback for 0005_link_part.sql, exact reverse-of-creation order (see
-- 0001_schema.rollback.sql for why the ordering matters positionally). Dropping
-- the column alone would take the constraint with it; both steps are spelled out
-- so each rollback step pairs with the apply step it undoes.

ALTER TABLE {schema}.links DROP CONSTRAINT links_part_iff_partial;

ALTER TABLE {schema}.links DROP COLUMN part;
