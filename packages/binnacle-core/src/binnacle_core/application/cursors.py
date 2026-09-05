"""Opaque pagination cursors.

The wire form is deliberately an opaque string rather than a typed value:
keyset cursors (this store) carry a sort value plus tiebreaker, while a
different backend's cursor could be a key map or a driver-supplied blob. A
string accommodates both; a typed cursor would make such a change breaking.

The payload carries an explicit value-type tag (`"vt"`) rather than inferring
the type of `v` by inspection. Inferring is undecidable in general: an
ISO-8601 datetime string and an arbitrary sort key that happens to also be a
string (e.g. `open_queue()`'s `domain` ordering) are indistinguishable by
shape alone. Tagging the type at encode time and dispatching on that tag at
decode time makes the codec total, and keeps it that way for any future
non-datetime, non-numeric sort key.

The payload also carries an optional *second* sort value (`"vt2"`/`"v2"`),
tagged and decoded the same way as the first. This is generic, not a
`shakiest`-specific bolt-on: most orderings here are a single leading value
plus an id tiebreaker, but `open_queue()`'s `shakiest` ordering is a genuine
three-column composite (leading COALESCE expression, then `proposed_at`, then
`item_id`), so its cursor must carry all three components to be replayable
without silently skipping rows tied on the leading value (see
docs/superpowers/specs/2026-09-05-binnacle-core-query-additions-design.md
§3.2). `value2` defaults to `None` and is simply unused by callers whose
ordering has no middle component.
"""

import base64
import binascii
import json
from datetime import datetime

from binnacle_core.domain.errors import InvalidCursor

CursorValue = datetime | float | str | None


def _tag_value(value: CursorValue) -> tuple[str, str | float | None]:
    """Tag `value`'s type explicitly (`"dt"` / `"num"` / `"str"` / `"null"`)
    so `_untag_value` can dispatch on the tag rather than infer the type from
    the serialized form. Shared by both the leading and second cursor value."""
    if value is None:
        return "null", None
    if isinstance(value, datetime):
        return "dt", value.isoformat()
    if isinstance(value, str):
        return "str", value
    return "num", value


def _untag_value(vt: object, raw_value: object, *, cursor: str, field: str) -> CursorValue:
    """Reverse of `_tag_value`. Raises `InvalidCursor` on a missing/unrecognized
    tag or a value that doesn't match its own declared tag."""
    if vt == "null":
        if raw_value is not None:
            raise InvalidCursor(f"cursor tagged null carries a {field}: {cursor[:32]!r}")
        return None
    if vt == "dt":
        if not isinstance(raw_value, str):
            raise InvalidCursor(f"cursor tagged dt carries a non-string {field}: {cursor[:32]!r}")
        try:
            return datetime.fromisoformat(raw_value)
        except ValueError as exc:
            raise InvalidCursor(f"cursor carries an unparseable {field}: {cursor[:32]!r}") from exc
    if vt == "num":
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            # `bool` is a subclass of `int` in Python -- without this guard a
            # stray JSON `true`/`false` in a hand-crafted/corrupt cursor would
            # silently become `1.0`/`0.0`.
            raise InvalidCursor(f"cursor tagged num carries a non-numeric {field}: {cursor[:32]!r}")
        return float(raw_value)
    if vt == "str":
        if not isinstance(raw_value, str):
            raise InvalidCursor(f"cursor tagged str carries a non-string {field}: {cursor[:32]!r}")
        return raw_value
    raise InvalidCursor(f"cursor carries an unrecognized {field} type {vt!r}: {cursor[:32]!r}")


def encode_cursor(
    *,
    sort: str,
    order: str,
    value: CursorValue,
    value2: CursorValue = None,
    tiebreaker: str,
) -> str:
    """Mint a cursor for the last row of a page. `value` is that row's leading
    sort-key value, `value2` an optional middle sort-key value (only
    `shakiest` currently uses this -- everything else leaves it `None`), and
    `tiebreaker` its id (as `str`). Each value's type is tagged explicitly
    (`"dt"` / `"num"` / `"str"` / `"null"`) so `decode_cursor` can dispatch on
    the tag rather than infer the type from the serialized form."""
    vt, v = _tag_value(value)
    vt2, v2 = _tag_value(value2)
    payload = {"s": sort, "o": order, "vt": vt, "v": v, "vt2": vt2, "v2": v2, "t": tiebreaker}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, sort: str, order: str) -> tuple[CursorValue, CursorValue, str]:
    """Reverse of `encode_cursor`, refusing a cursor minted under a different
    ordering. Raises `InvalidCursor` on malformed input or a mismatch. Returns
    `(value, value2, tiebreaker)` -- `value2` is `None` for cursors minted
    without a second component (i.e. every ordering except `shakiest`)."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, binascii.Error) as exc:
        raise InvalidCursor(f"cursor is not decodable: {cursor[:32]!r}") from exc
    if not isinstance(payload, dict):
        raise InvalidCursor(f"cursor payload is not an object: {cursor[:32]!r}")
    if payload.get("s") != sort or payload.get("o") != order:
        raise InvalidCursor(
            f"cursor was minted for sort={payload.get('s')!r} order={payload.get('o')!r}, "
            f"replayed under sort={sort!r} order={order!r}"
        )
    value = _untag_value(payload.get("vt"), payload.get("v"), cursor=cursor, field="value")
    # "vt2"/"v2" are absent on a cursor minted before this second component
    # existed (or by an ordering that never sets it) -- default to the tag for
    # "no second value" rather than treating absence as malformed.
    value2 = _untag_value(
        payload.get("vt2", "null"), payload.get("v2"), cursor=cursor, field="second value"
    )
    tiebreaker = payload.get("t")
    if not isinstance(tiebreaker, str):
        raise InvalidCursor(f"cursor carries no tiebreaker: {cursor[:32]!r}")
    return value, value2, tiebreaker
