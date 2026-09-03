-- Rollback for 0001_schema.sql. Written in exact reverse-of-creation order: yoyo
-- pairs step N's rollback with step N's apply positionally (see migrations.py
-- Migration.load()), so each DROP here undoes the CREATE directly above it once
-- the file is read top-to-bottom in reverse.

DROP TABLE {schema}.domain_transitions;
DROP TABLE {schema}.embeddings;
DROP TABLE {schema}.queue;
DROP TABLE {schema}.transitions;
DROP TABLE {schema}.refs;
DROP TABLE {schema}.links;
DROP TABLE {schema}.decisions;
DROP TABLE {schema}.domains;
DROP SCHEMA IF EXISTS {schema};
