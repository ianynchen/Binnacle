"""`BinnacleConfig` (docs/components/01-configuration-and-client.md): the single
caller-constructed config object the library takes (FR-8.1) — no env/file/global
reads, fail-at-construction validation, multiple independently configured
instances coexist (nothing here is process-global).
"""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from binnacle_core.application.ports import Embedder, Suggester
from binnacle_core.domain.errors import ConfigError

# FR-7.4 discovery default: top-k neighbors considered per newly embedded
# decision, capped at 10 (REQUIREMENTS FR-7.4 "k configurable, default ≤10").
_MAX_DISCOVERY_K = 10

# `schema_name` is interpolated directly into DDL/DML f-strings in
# `adapters.postgres_store` (identifiers can't be bound as query params in
# psycopg — Postgres has no parameter syntax for a schema-qualified table
# name), so an unvalidated value is a SQL-injection vector. Restricted to what
# Postgres accepts unquoted as an identifier anyway (lowercase-normalized,
# `[a-z_][a-z0-9_]*`, <=63 bytes) — deliberately duplicated in
# `PostgresStore.__init__` (see that module) since the store is constructible
# directly, bypassing this config object entirely.
_SCHEMA_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class DiscoveryConfig(BaseModel):
    """FR-7.4 discovery sweep knobs: `k` nearest neighbors considered per
    decision (capped at 10), the confidence floor below which a `Suggester`
    classification is dropped, and the per-sweep cap on queue items a single
    `discover()` run may enqueue (keeps the review queue bounded regardless of
    engine enthusiasm, REQUIREMENTS FR-7.4)."""

    k: int = 10
    confidence_floor: float = 0.6
    per_sweep_cap: int = 50

    @model_validator(mode="after")
    def _validate_caps(self) -> "DiscoveryConfig":
        if not (1 <= self.k <= _MAX_DISCOVERY_K):
            msg = f"discovery.k must be between 1 and {_MAX_DISCOVERY_K}, got {self.k}"
            raise ConfigError(msg)
        if not (0.0 <= self.confidence_floor <= 1.0):
            msg = f"discovery.confidence_floor must be between 0.0 and 1.0, got {self.confidence_floor}"
            raise ConfigError(msg)
        if self.per_sweep_cap < 1:
            msg = f"discovery.per_sweep_cap must be >= 1, got {self.per_sweep_cap}"
            raise ConfigError(msg)
        return self


class BinnacleConfig(BaseModel):
    """docs/components/01-configuration-and-client.md VERBATIM. Constructing this
    object performs no I/O — it only validates shape (dsn XOR pool, discovery
    caps); `Binnacle.__init__` builds the store from it, still without I/O."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    dsn: str | None = None
    pool: Any | None = None
    schema_name: str = "binnacle"
    embedder: Embedder
    suggester: Suggester | None = None
    embedding_dim: int = 768
    archival_age_days: int = 90
    compact_outcome_chars: int = 200
    discovery: DiscoveryConfig = DiscoveryConfig()

    @model_validator(mode="after")
    def _validate_dsn_xor_pool(self) -> "BinnacleConfig":
        if (self.dsn is None) == (self.pool is None):
            msg = "BinnacleConfig requires exactly one of dsn or pool"
            raise ConfigError(msg)
        return self

    @model_validator(mode="after")
    def _validate_schema_name(self) -> "BinnacleConfig":
        if not _SCHEMA_NAME_RE.match(self.schema_name):
            msg = (
                f"schema_name {self.schema_name!r} is not a valid identifier "
                f"(must match {_SCHEMA_NAME_RE.pattern!r})"
            )
            raise ConfigError(msg)
        return self
