"""Opaque pagination cursors.

The wire form is deliberately an opaque string rather than a typed value:
keyset cursors (this store) carry a sort value plus tiebreaker, while a
different backend's cursor could be a key map or a driver-supplied blob. A
string accommodates both; a typed cursor would make such a change breaking.
"""

import base64
import binascii
import json
from datetime import datetime

from binnacle_core.domain.errors import InvalidCursor


def encode_cursor(*, sort: str, order: str, value: datetime | float | None, tiebreaker: str) -> str:
    """Mint a cursor for the last row of a page. `value` is that row's sort-key
    value, `tiebreaker` its id (as `str`). A `datetime` is serialized via
    `.isoformat()`; a numeric value (e.g. `queue()`'s `shakiest` confidence
    ordering) is serialized directly, since JSON already round-trips floats."""
    payload = {
        "s": sort,
        "o": order,
        "v": value.isoformat() if isinstance(value, datetime) else value,
        "t": tiebreaker,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, sort: str, order: str) -> tuple[datetime | float | None, str]:
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
    raw_value = payload.get("v")
    value: datetime | float | None
    if raw_value is None:
        value = None
    elif isinstance(raw_value, str):
        try:
            value = datetime.fromisoformat(raw_value)
        except ValueError as exc:
            raise InvalidCursor(f"cursor carries an unparseable value: {cursor[:32]!r}") from exc
    elif isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
        value = float(raw_value)
    else:
        raise InvalidCursor(f"cursor carries an unsupported value: {cursor[:32]!r}")
    tiebreaker = payload.get("t")
    if not isinstance(tiebreaker, str):
        raise InvalidCursor(f"cursor carries no tiebreaker: {cursor[:32]!r}")
    return value, tiebreaker
