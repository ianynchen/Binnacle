"""A cursor must survive a round trip and must refuse to be replayed under a
different ordering -- replaying it silently would return a page computed
against the wrong sort, a wrongness with no visible symptom.

The payload tags its value's type explicitly (`vt`: `"dt"`/`"num"`/`"str"`/
`"null"`) rather than having `decode_cursor` infer the type from `v`'s shape
-- an ISO-8601 datetime string and an arbitrary string sort key (e.g.
`open_queue()`'s `domain` ordering) are indistinguishable by shape alone, so
several tests below build a payload by hand to prove `decode_cursor` is
total under a mismatched or unrecognized tag, not just under the tags
`encode_cursor` itself would ever produce."""

import base64
import json
import string
from datetime import UTC, datetime

import pytest

from binnacle_core import InvalidCursor
from binnacle_core.application.cursors import decode_cursor, encode_cursor


def _cursor_with_raw_v(raw_v: object, *, vt: object = "dt") -> str:
    """Build a cursor payload by hand, bypassing `encode_cursor`, so `v` (and
    its declared `vt` tag) can hold a combination `encode_cursor` itself
    could never produce."""
    payload = {"s": "recorded_at", "o": "desc", "vt": vt, "v": raw_v, "t": "abc"}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_round_trips_a_datetime_sort_value() -> None:
    at = datetime(2021, 3, 14, 9, 22, 11, tzinfo=UTC)
    token = encode_cursor(sort="recorded_at", order="desc", value=at, tiebreaker="abc")
    assert decode_cursor(token, sort="recorded_at", order="desc") == (at, "abc")


def test_round_trips_a_null_sort_value() -> None:
    token = encode_cursor(sort="valid_until", order="asc", value=None, tiebreaker="abc")
    assert decode_cursor(token, sort="valid_until", order="asc") == (None, "abc")


def test_round_trips_a_numeric_sort_value() -> None:
    """`queue()`'s `shakiest` ordering leads with a numeric (confidence)
    expression rather than a datetime -- the codec must round-trip that too."""
    token = encode_cursor(sort="shakiest", order="asc", value=0.42, tiebreaker="7")
    assert decode_cursor(token, sort="shakiest", order="asc") == (0.42, "7")


def test_round_trips_a_string_sort_value_that_is_not_a_valid_date() -> None:
    """`queue()`'s `domain` ordering leads with a domain name -- a plain
    string that is not, and must not be required to look like, an ISO-8601
    date. Before the `vt` tag existed, `decode_cursor` tried
    `datetime.fromisoformat()` on every string `v`, so a value like this one
    would always raise `InvalidCursor`."""
    token = encode_cursor(sort="domain", order="asc", value="architecture", tiebreaker="7")
    assert decode_cursor(token, sort="domain", order="asc") == ("architecture", "7")


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
    """A `v` tagged `dt` that is a string but not a valid ISO date must still
    raise InvalidCursor rather than an uncaught ValueError -- an escaping
    ValueError surfaces to binnacle-router as an unhandled 500 instead of the
    controlled error response InvalidCursor is meant to produce."""
    token = _cursor_with_raw_v("not-a-date", vt="dt")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="desc")


def test_rejects_a_cursor_whose_tagged_type_does_not_match_its_value() -> None:
    """A `v` that doesn't match its own declared `vt` (e.g. tagged `num` but
    actually a JSON array) must be refused, not silently coerced -- silently
    accepting it would page from the wrong position with no visible symptom."""
    token = _cursor_with_raw_v([1, 2], vt="num")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="desc")


def test_rejects_a_cursor_with_a_boolean_value() -> None:
    """`bool` is a subclass of `int` in Python -- a naive numeric check would
    silently accept a JSON `true`/`false` tagged `num` as `1.0`/`0.0`."""
    token = _cursor_with_raw_v(True, vt="num")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="desc")


def test_rejects_a_cursor_with_an_unrecognized_value_type_tag() -> None:
    """`decode_cursor` dispatches on `vt` rather than inferring from `v`'s
    shape -- an unrecognized tag (or one from a future/foreign cursor format)
    must be refused outright rather than falling through to a guess."""
    token = _cursor_with_raw_v("whatever", vt="bogus")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="desc")


def test_rejects_a_cursor_with_no_value_type_tag() -> None:
    """A payload built without a `vt` key at all (e.g. a pre-tag cursor, or
    one crafted by hand) must be refused rather than silently defaulting to
    some type."""
    payload = {"s": "recorded_at", "o": "desc", "v": "2021-03-14", "t": "abc"}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    with pytest.raises(InvalidCursor):
        decode_cursor(token, sort="recorded_at", order="desc")


def test_cursor_is_url_safe_so_it_survives_a_query_string() -> None:
    """binnacle-router will carry this in a URL, so the alphabet matters:
    '+' and '/' from standard base64 would need escaping."""
    token = encode_cursor(sort="recorded_at", order="desc", value=None, tiebreaker="abc")
    assert set(token) <= set(string.ascii_letters + string.digits + "-_")
