"""`PostgresStore.__init__` validation -- pure construction, no I/O, so these
run without a live Postgres (unlike everything else that touches the store).

Covers the `schema_name` SQL-injection guard duplicated here on top of
`BinnacleConfig`'s own validator (tests/unit/test_config.py
TestSchemaNameValidation): `PostgresStore` is constructible directly,
bypassing `BinnacleConfig` entirely, so it must not trust an unvalidated
caller.
"""

import pytest

from binnacle_core.adapters.postgres_store import PostgresStore
from binnacle_core.domain.errors import ConfigError


class TestSchemaNameValidation:
    def test_hostile_value_rejected(self) -> None:
        hostile = "binj; CREATE TABLE public.pwned_by_config (x int); --"
        with pytest.raises(ConfigError):
            PostgresStore(dsn="postgresql://x", schema_name=hostile)

    def test_uppercase_rejected(self) -> None:
        with pytest.raises(ConfigError):
            PostgresStore(dsn="postgresql://x", schema_name="Binnacle")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ConfigError):
            PostgresStore(dsn="postgresql://x", schema_name="")

    def test_default_schema_name_accepted(self) -> None:
        PostgresStore(dsn="postgresql://x")  # does not raise

    def test_legal_custom_name_accepted(self) -> None:
        PostgresStore(dsn="postgresql://x", schema_name="tenant_a")  # does not raise


class TestDsnXorPool:
    def test_neither_rejected(self) -> None:
        with pytest.raises(ConfigError):
            PostgresStore()

    def test_both_rejected(self) -> None:
        with pytest.raises(ConfigError):
            PostgresStore(dsn="postgresql://x", pool=object())  # type: ignore[arg-type]
