"""A cursor must survive a round trip and must refuse to be replayed under a
different ordering -- replaying it silently would return a page computed
against the wrong sort, a wrongness with no visible symptom."""

import base64
import json
import string
from datetime import UTC, datetime

import pytest

from binnacle_core import InvalidCursor
from binnacle_core.application.cursors import decode_cursor, encode_cursor


def _cursor_with_raw_v(raw_v: object) -> str:
    """Build a cursor payload by hand, bypassing `encode_cursor`, so `v` can
    hold a value `encode_cursor` itself could never produce."""
    payload = {"s": "recorded_at", "o": "desc", "v": raw_v, "t": "abc"}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


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


def test_rejects_a_cursor_with_a_corrupt_date_string() -> None:
    """A `v` that is a string but not a valid ISO date must still raise
    InvalidCursor rather than an uncaught ValueError -- an escaping ValueError
    surfaces to binnacle-router as an unhandled 500 instead of the controlled
    error response InvalidCursor is meant to produce."""
    token = _cursor_with_raw_v("not-a-date")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="desc")


def test_round_trips_a_numeric_sort_value() -> None:
    """`queue()`'s `shakiest` ordering leads with a numeric (confidence)
    expression rather than a datetime -- the codec must round-trip that too."""
    token = encode_cursor(sort="shakiest", order="asc", value=0.42, tiebreaker="7")
    assert decode_cursor(token, sort="shakiest", order="asc") == (0.42, "7")


def test_rejects_a_cursor_with_a_non_scalar_value() -> None:
    """A `v` that is neither a string, a number, nor null (e.g. a JSON array)
    must be refused, not silently coerced to None -- silently accepting it
    would page from the wrong position with no visible symptom."""
    token = _cursor_with_raw_v([1, 2])
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="desc")


def test_rejects_a_cursor_with_a_boolean_value() -> None:
    """`bool` is a subclass of `int` in Python -- a naive numeric check would
    silently accept a JSON `true`/`false` as `1.0`/`0.0`."""
    token = _cursor_with_raw_v(True)
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="desc")


def test_cursor_is_url_safe_so_it_survives_a_query_string() -> None:
    """binnacle-router will carry this in a URL, so the alphabet matters:
    '+' and '/' from standard base64 would need escaping."""
    token = encode_cursor(sort="recorded_at", order="desc", value=None, tiebreaker="abc")
    assert set(token) <= set(string.ascii_letters + string.digits + "-_")
