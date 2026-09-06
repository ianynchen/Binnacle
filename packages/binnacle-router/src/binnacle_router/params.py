"""Shared query-parameter helpers used across route modules."""


def paired(
    kind: str | None,
    identifier: str | None,
    *,
    kind_param: str,
    identifier_param: str,
) -> tuple[str, str] | None:
    """Pair a kind/identifier query-parameter pair into the `(kind, identifier)`
    tuple a filter parameter expects (e.g. `subject`, `evidence`, `actor`).
    Supplying only one half is meaningless and would otherwise be silently
    dropped, widening the query -- so it raises, which `BinnacleAPIRoute` in
    `errors.py` turns into a 422. Shared across route modules
    (`decisions.py`, `feeds.py`) rather than duplicated.

    `kind_param`/`identifier_param` are the query parameter names the calling
    endpoint actually declares, and the message quotes them verbatim: the two
    halves are not named to a single pattern across the surface
    (`subject_kind`/`subject_identifier` in `decisions.py`, but
    `actor_kind`/`actor_id` in `feeds.py` -- REQUIREMENTS FR-4.5), so a
    message derived from one shared stem told `/changes` clients to supply an
    `actor_identifier` that does not exist. A client obeying that advice gets
    the identical 422 forever, since unknown query parameters are ignored."""
    if (kind is None) != (identifier is None):
        msg = f"{kind_param} and {identifier_param} must be supplied together"
        raise ValueError(msg)
    return None if kind is None else (kind, identifier)  # type: ignore[return-value]
