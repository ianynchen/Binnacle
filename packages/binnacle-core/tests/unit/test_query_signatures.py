"""relevant() and relevant_count() must accept the same filters forever.

If a filter is added to one and forgotten on the other, counts silently
disagree with the pages they describe -- wrong with no symptom until someone
notices the totals are off. This test is the guard (GUIDELINES §8: rules are
enforced, not aspirational)."""

import inspect

from binnacle_core import Binnacle

PRESENTATION_PARAMS = {"self", "sort", "order", "after", "limit", "projection"}


def test_relevant_count_accepts_every_relevant_filter() -> None:
    relevant_filters = set(inspect.signature(Binnacle.relevant).parameters) - PRESENTATION_PARAMS
    count_filters = set(inspect.signature(Binnacle.relevant_count).parameters) - {"self"}
    assert relevant_filters == count_filters
