"""Unit tests for `BinnacleConfig`/`DiscoveryConfig` (docs/components/01
"Acceptance": construction matrix — dsn-xor-pool, bad discovery caps rejected,
two instances with different schemas coexist). No I/O: construction is pure
validation (FR-8.1's "config-object initialization" — fail at construction,
never at first use).
"""

import pytest

from binnacle_core.application.config import BinnacleConfig, DiscoveryConfig
from binnacle_core.domain.errors import ConfigError
from tests.helpers import StubEmbedder


def _embedder() -> StubEmbedder:
    return StubEmbedder(dim=8)


class TestDsnXorPool:
    def test_neither_dsn_nor_pool_rejected(self) -> None:
        with pytest.raises(ConfigError):
            BinnacleConfig(embedder=_embedder())

    def test_both_dsn_and_pool_rejected(self) -> None:
        with pytest.raises(ConfigError):
            BinnacleConfig(dsn="postgresql://x", pool=object(), embedder=_embedder())

    def test_dsn_only_accepted(self) -> None:
        config = BinnacleConfig(dsn="postgresql://x", embedder=_embedder())
        assert config.dsn == "postgresql://x"
        assert config.pool is None

    def test_pool_only_accepted(self) -> None:
        pool = object()
        config = BinnacleConfig(pool=pool, embedder=_embedder())
        assert config.pool is pool
        assert config.dsn is None


class TestEmbedderRequired:
    def test_missing_embedder_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 - pydantic's own required-field error
            BinnacleConfig(dsn="postgresql://x")  # type: ignore[call-arg]

    def test_suggester_optional(self) -> None:
        config = BinnacleConfig(dsn="postgresql://x", embedder=_embedder())
        assert config.suggester is None


class TestDefaults:
    def test_compact_outcome_chars_defaults_to_200(self) -> None:
        config = BinnacleConfig(dsn="postgresql://x", embedder=_embedder())
        assert config.compact_outcome_chars == 200

    def test_schema_name_defaults_to_binnacle(self) -> None:
        config = BinnacleConfig(dsn="postgresql://x", embedder=_embedder())
        assert config.schema_name == "binnacle"

    def test_embedding_dim_defaults_to_768(self) -> None:
        config = BinnacleConfig(dsn="postgresql://x", embedder=_embedder())
        assert config.embedding_dim == 768

    def test_archival_age_days_defaults_to_90(self) -> None:
        config = BinnacleConfig(dsn="postgresql://x", embedder=_embedder())
        assert config.archival_age_days == 90

    def test_discovery_defaults(self) -> None:
        config = BinnacleConfig(dsn="postgresql://x", embedder=_embedder())
        assert config.discovery == DiscoveryConfig(k=10, confidence_floor=0.6, per_sweep_cap=50)


class TestDiscoveryCaps:
    def test_k_above_ten_rejected(self) -> None:
        with pytest.raises(ConfigError):
            BinnacleConfig(
                dsn="postgresql://x", embedder=_embedder(), discovery=DiscoveryConfig(k=11)
            )

    def test_k_below_one_rejected(self) -> None:
        with pytest.raises(ConfigError):
            DiscoveryConfig(k=0)

    def test_confidence_floor_out_of_range_rejected(self) -> None:
        with pytest.raises(ConfigError):
            DiscoveryConfig(confidence_floor=1.5)

    def test_negative_confidence_floor_rejected(self) -> None:
        with pytest.raises(ConfigError):
            DiscoveryConfig(confidence_floor=-0.1)

    def test_per_sweep_cap_below_one_rejected(self) -> None:
        with pytest.raises(ConfigError):
            DiscoveryConfig(per_sweep_cap=0)

    def test_valid_caps_accepted(self) -> None:
        config = DiscoveryConfig(k=10, confidence_floor=0.0, per_sweep_cap=1)
        assert config.k == 10


class TestSchemaNameValidation:
    """`schema_name` is interpolated directly into DDL/DML f-strings in
    `adapters.postgres_store` (SQL injection if unvalidated) -- construction
    must reject anything that isn't a plain lowercase identifier, matching
    what Postgres accepts unquoted."""

    def test_hostile_value_rejected(self) -> None:
        hostile = "binj; CREATE TABLE public.pwned_by_config (x int); --"
        with pytest.raises(ConfigError):
            BinnacleConfig(dsn="postgresql://x", embedder=_embedder(), schema_name=hostile)

    def test_double_quote_rejected(self) -> None:
        with pytest.raises(ConfigError):
            BinnacleConfig(dsn="postgresql://x", embedder=_embedder(), schema_name='foo"; --')

    def test_uppercase_rejected(self) -> None:
        with pytest.raises(ConfigError):
            BinnacleConfig(dsn="postgresql://x", embedder=_embedder(), schema_name="Binnacle")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ConfigError):
            BinnacleConfig(dsn="postgresql://x", embedder=_embedder(), schema_name="")

    def test_leading_digit_rejected(self) -> None:
        with pytest.raises(ConfigError):
            BinnacleConfig(dsn="postgresql://x", embedder=_embedder(), schema_name="1tenant")

    def test_default_schema_name_accepted(self) -> None:
        config = BinnacleConfig(dsn="postgresql://x", embedder=_embedder())
        assert config.schema_name == "binnacle"

    def test_legal_custom_names_accepted(self) -> None:
        for name in ("tenant_a", "_private", "a" * 63, "tenant2"):
            config = BinnacleConfig(dsn="postgresql://x", embedder=_embedder(), schema_name=name)
            assert config.schema_name == name

    def test_name_over_63_bytes_rejected(self) -> None:
        with pytest.raises(ConfigError):
            BinnacleConfig(dsn="postgresql://x", embedder=_embedder(), schema_name="a" * 64)


class TestMultipleInstancesCoexist:
    """FR-8.1: "multiple independently configured instances may coexist" — no
    global/process state, so two configs never interfere with each other."""

    def test_two_configs_different_schemas_are_independent(self) -> None:
        config_a = BinnacleConfig(
            dsn="postgresql://x", schema_name="tenant_a", embedder=_embedder()
        )
        config_b = BinnacleConfig(
            dsn="postgresql://x", schema_name="tenant_b", embedder=_embedder()
        )
        assert config_a.schema_name == "tenant_a"
        assert config_b.schema_name == "tenant_b"
        # Mutating one instance's nested discovery config never leaks into the
        # other's — each BaseModel default is its own object, not shared state.
        config_a.discovery.k = 3
        assert config_b.discovery.k == 10

    def test_two_configs_different_embedders_are_independent(self) -> None:
        embedder_a = StubEmbedder(dim=8)
        embedder_b = StubEmbedder(dim=16)
        config_a = BinnacleConfig(dsn="postgresql://x", embedder=embedder_a)
        config_b = BinnacleConfig(dsn="postgresql://x", embedder=embedder_b)
        assert config_a.embedder is embedder_a
        assert config_b.embedder is embedder_b
