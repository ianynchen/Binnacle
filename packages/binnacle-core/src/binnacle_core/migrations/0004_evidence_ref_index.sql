-- Evidence-ref lookups (REQUIREMENTS FR-6.1's `evidence` filter, added
-- 2026-09-05). idx_refs_subject is partial on `WHERE role = 'subject'`, so it
-- cannot serve a role='evidence' lookup at all -- an evidence filter without
-- this index is a sequential scan of `refs`.

CREATE INDEX idx_refs_evidence ON {schema}.refs(kind, identifier) WHERE role = 'evidence';
