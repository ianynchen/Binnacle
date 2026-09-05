"""The public surface must be closed under the signatures it exposes.

A method is only usable if a caller can name what it returns. binnacle-router
imports exclusively from this top-level package (its import-linter contract
forbids reaching into submodules), so a return type that is not re-exported
makes its method effectively unusable from outside."""

import binnacle_core


def test_every_type_named_by_a_public_signature_is_importable() -> None:
    for name in (
        "ArchivalSummary",
        "BackfillSummary",
        "DiscoverySummary",
        "PrecedentHit",
        "Tier",
    ):
        assert hasattr(binnacle_core, name), (
            f"{name} is returned by a public method but not exported"
        )
        assert name in binnacle_core.__all__, f"{name} is importable but missing from __all__"
