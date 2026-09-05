"""Sub-routers grouped by resource, `include_router`-ed into `make_router`."""


def _paired(kind: str | None, identifier: str | None, *, name: str) -> tuple[str, str] | None:
    """Pair a `{name}_kind`/`{name}_identifier` query-parameter pair into the
    `(kind, identifier)` tuple a filter parameter expects (e.g. `subject`,
    `evidence`, `actor`). Supplying only one half is meaningless and would
    otherwise be silently dropped, widening the query -- so it raises, which
    the `ValueError` handler in `errors.py` turns into a 422. Shared across
    route modules (`decisions.py`, `feeds.py`) rather than duplicated."""
    if (kind is None) != (identifier is None):
        msg = f"{name}_kind and {name}_identifier must be supplied together"
        raise ValueError(msg)
    return None if kind is None else (kind, identifier)  # type: ignore[return-value]
