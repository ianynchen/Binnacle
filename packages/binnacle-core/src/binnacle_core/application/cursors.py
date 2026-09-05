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
"""

import base64
import binascii
import json
from datetime import datetime

from binnacle_core.domain.errors import InvalidCursor

CursorValue = datetime | float | str | None


def encode_cursor(*, sort: str, order: str, value: CursorValue, tiebreaker: str) -> str:
    """Mint a cursor for the last row of a page. `value` is that row's sort-key
    value, `tiebreaker` its id (as `str`). The payload tags `value`'s type
    explicitly (`"dt"` / `"num"` / `"str"` / `"null"`) so `decode_cursor` can
    dispatch on the tag rather than infer the type from the serialized form."""
    vt: str
    v: str | float | None
    if value is None:
        vt, v = "null", None
    elif isinstance(value, datetime):
        vt, v = "dt", value.isoformat()
    elif isinstance(value, str):
        vt, v = "str", value
    else:
        vt, v = "num", value
    payload = {"s": sort, "o": order, "vt": vt, "v": v, "t": tiebreaker}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, sort: str, order: str) -> tuple[CursorValue, str]:
    """Reverse of `encode_cursor`, refusing a cursor minted under a different
    ordering. Raises `InvalidCursor` on malformed input or a mismatch."""
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
    vt = payload.get("vt")
    raw_value = payload.get("v")
    value: CursorValue
    if vt == "null":
        if raw_value is not None:
            raise InvalidCursor(f"cursor tagged null carries a value: {cursor[:32]!r}")
        value = None
    elif vt == "dt":
        if not isinstance(raw_value, str):
            raise InvalidCursor(f"cursor tagged dt carries a non-string value: {cursor[:32]!r}")
        try:
            value = datetime.fromisoformat(raw_value)
        except ValueError as exc:
            raise InvalidCursor(f"cursor carries an unparseable value: {cursor[:32]!r}") from exc
    elif vt == "num":
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            # `bool` is a subclass of `int` in Python -- without this guard a
            # stray JSON `true`/`false` in a hand-crafted/corrupt cursor would
            # silently become `1.0`/`0.0`.
            raise InvalidCursor(f"cursor tagged num carries a non-numeric value: {cursor[:32]!r}")
        value = float(raw_value)
    elif vt == "str":
        if not isinstance(raw_value, str):
            raise InvalidCursor(f"cursor tagged str carries a non-string value: {cursor[:32]!r}")
        value = raw_value
    else:
        raise InvalidCursor(f"cursor carries an unrecognized value type {vt!r}: {cursor[:32]!r}")
    tiebreaker = payload.get("t")
    if not isinstance(tiebreaker, str):
        raise InvalidCursor(f"cursor carries no tiebreaker: {cursor[:32]!r}")
    return value, tiebreaker
