-- Binnacle schema — tables (ARCHITECTURE.md §4).
--
-- Schema-qualification: `{schema}` and `{embedding_dim}` are literal placeholder
-- tokens substituted via plain string replacement (binnacle.adapters.postgres_store
-- ._render_migrations) before this file ever reaches yoyo — NOT str.format(), since
-- the JSONB defaults below contain literal `{}` that .format() would misparse as a
-- field reference. The schema itself must exist before yoyo connects (it sets
-- search_path to it so yoyo's own bookkeeping tables land inside the schema too),
-- so PostgresStore.migrate() creates it in a preflight step; the CREATE SCHEMA
-- below is therefore belt-and-braces (IF NOT EXISTS) for anyone driving this file
-- outside that path.
--
-- Table order here is FK-safe (domains and decisions must exist before anything
-- that references them), which differs from ARCHITECTURE.md's prose order.

CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE {schema}.domains (
  name TEXT PRIMARY KEY, description TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE {schema}.decisions (
  decision_id     UUID PRIMARY KEY,            -- caller-supplied or minted (FR-1.6)
  tier            TEXT NOT NULL,               -- 'short_term' | 'long_term'
  domain          TEXT NOT NULL REFERENCES {schema}.domains(name),
  status          TEXT NOT NULL,               -- denormalized fold of transitions (I-1)
  scenario        TEXT NOT NULL,
  outcome         TEXT NOT NULL,
  reasoning       TEXT NOT NULL,
  options_considered JSONB NOT NULL DEFAULT '[]',   -- [{option, why_rejected}]
  consequences    TEXT,
  confidence      REAL,                        -- optional triage signal (FR-1.1)
  source          TEXT NOT NULL,
  content_hash    TEXT NOT NULL,               -- FR-1.6 idempotency key (ARCHITECTURE.md §4)
  recorded_by     TEXT NOT NULL,               -- attested actor "kind:id" (I-2)
  decided_at      TIMESTAMPTZ NOT NULL,        -- FR-1.7 (defaults to recorded_at)
  recorded_at     TIMESTAMPTZ NOT NULL,
  valid_from      TIMESTAMPTZ,
  valid_until     TIMESTAMPTZ,
  metadata        JSONB NOT NULL DEFAULT '{}',
  schema_version  INT NOT NULL DEFAULT 1
);

CREATE TABLE {schema}.links (                  -- all inter-decision relationships
  from_id UUID NOT NULL REFERENCES {schema}.decisions(decision_id),
  to_id   UUID NOT NULL REFERENCES {schema}.decisions(decision_id),
  kind    TEXT NOT NULL,                       -- 'SUPERSEDES' | 'SUPPLEMENTS' | 'PROMOTED_FROM'
  PRIMARY KEY (from_id, kind, to_id)
);

CREATE TABLE {schema}.refs (
  decision_id UUID NOT NULL REFERENCES {schema}.decisions(decision_id),
  role        TEXT NOT NULL,                   -- 'subject' | 'evidence'
  kind        TEXT NOT NULL,                   -- open: component, product, market, session, url, ...
  identifier  TEXT NOT NULL,
  note        TEXT,
  PRIMARY KEY (decision_id, role, kind, identifier)
);

CREATE TABLE {schema}.transitions (
  transition_id BIGSERIAL PRIMARY KEY,
  decision_id   UUID NOT NULL REFERENCES {schema}.decisions(decision_id),
  action        TEXT NOT NULL,                 -- recorded|recommended|promoted|declined|discarded|
                                               -- superseded|supplement_linked|archived|reactivated|...
  actor         TEXT NOT NULL,                 -- "kind:id" (kind ∈ human|agent|engine)
  at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  reason        TEXT,
  new_status    TEXT,                          -- resulting status when the action changes one;
                                               -- fold(transitions) = last non-null new_status (I-1)
  payload       JSONB                          -- {"target": ...}; {"item_id": ...} on resolutions
);

CREATE TABLE {schema}.queue (
  item_id     BIGSERIAL PRIMARY KEY,
  kind        TEXT NOT NULL,                   -- 'promote' | 'link' | 'supersede'
  decision_id UUID NOT NULL REFERENCES {schema}.decisions(decision_id),
  target_id   UUID REFERENCES {schema}.decisions(decision_id),
  proposed_by TEXT NOT NULL, proposed_at TIMESTAMPTZ NOT NULL,
  rationale   TEXT, confidence REAL,
  resolved    BOOLEAN NOT NULL DEFAULT FALSE   -- resolution detail lives in transitions
);

CREATE TABLE {schema}.embeddings (
  decision_id   UUID PRIMARY KEY REFERENCES {schema}.decisions(decision_id),
  embedding     VECTOR({embedding_dim}) NOT NULL,   -- dimension fixed by config at migration time
  embedded_at   TIMESTAMPTZ NOT NULL,
  discovered_at TIMESTAMPTZ                    -- discovery cursor: NULL = not yet swept (FR-7.4);
                                               -- over-cap rows stay NULL, picked up next sweep
);

CREATE TABLE {schema}.domain_transitions (     -- FR-2.2 registry audit (no decision row to attach to)
  id BIGSERIAL PRIMARY KEY, domain TEXT NOT NULL, action TEXT NOT NULL,
  actor TEXT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now(), reason TEXT
);
