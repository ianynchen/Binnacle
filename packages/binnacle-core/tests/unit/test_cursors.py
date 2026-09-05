"""A cursor must survive a round trip and must refuse to be replayed under a
different ordering -- replaying it silently would return a page computed
against the wrong sort, a wrongness with no visible symptom."""

import string
from datetime import UTC, datetime

import pytest

from binnacle_core import InvalidCursor
from binnacle_core.application.cursors import decode_cursor, encode_cursor


def test_round_trips_value_and_tiebreaker() -> None:
    at = datetime(2021, 3, 14, 9, 22, 11, tzinfo=UTC)
    token = encode_cursor(sort="recorded_at", order="desc", value=at, tiebreaker="abc")
    assert decode_cursor(token, sort="recorded_at", order="desc") == (at, "abc")


def test_round_trips_a_null_sort_value() -> None:
    token = encode_cursor(sort="valid_until", order="asc", value=None, tiebreaker="abc")
    assert decode_cursor(token, sort="valid_until", order="asc") == (None, "abc")


def test_rejects_a_cursor_minted_under_a_different_sort() -> None:
    token = encode_cursor(sort="recorded_at", order="desc", value=None, tiebreaker="abc")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="decided_at", order="desc")


def test_rejects_a_cursor_minted_under_a_different_direction() -> None:
    token = encode_cursor(sort="recorded_at", order="desc", value=None, tiebreaker="abc")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="asc")


def test_rejects_a_malformed_cursor_rather_than_falling_back_to_page_one() -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor("not-base64-at-all!!", sort="recorded_at", order="desc")


def test_cursor_is_url_safe_so_it_survives_a_query_string() -> None:
    """binnacle-router will carry this in a URL, so the alphabet matters:
    '+' and '/' from standard base64 would need escaping."""
    token = encode_cursor(sort="recorded_at", order="desc", value=None, tiebreaker="abc")
    assert set(token) <= set(string.ascii_letters + string.digits + "-_")
