"""The precedent pipeline (docs/binnacle-core/components/04-query-and-assist.md "Query
contracts" / "precedent()"; REQUIREMENTS FR-6.3): embed the question -> k-NN
against `embeddings` -> attribute filters -> hydrate compact projections in
similarity order, each carrying its raw score.

This module owns exactly the one query that is genuine multi-step
*composition* over the store (embed, then knn, then filter, then hydrate).
`relevant`/`history`/`changes`/`queue`/`get_many`/`by_source`/`export` (Task 6)
stay as single-call delegations directly on `Binnacle` -- each is already one
store call with no shaping in between, so routing them through here would add
an indirection layer without adding cohesion. Only `precedent()` earns a
dedicated module (same free-function-over-`StorePort` shape as
`recorder.insert_new_decision`, not a class -- there is no state to hold
between calls).
"""

from collections.abc import Sequence
from uuid import UUID

from binnacle_core.application.ports import Embedder, StorePort
from binnacle_core.domain.models import PrecedentHit, Tier

# `store.knn` already protects itself against archived/discarded starving the
# result (over-fetches k*4 internally, per StorePort.knn). `domains`/`tiers`/
# `include_dead=False` are filters knn cannot apply itself (it only knows the
# vector index, not decision attributes) -- applying the SAME multiplier here,
# on top of the caller's `limit`, keeps `precedent()`'s first round consistent
# with the store's own documented factor rather than inventing a second knob.
#
# A single round at this factor can still under-fill (a filter that rejects
# more than 3 out of 4 candidates leaves fewer than `limit` survivors even
# though the index holds more matches) -- `precedent()` escalates: each round
# that under-fills AND came back with a full batch (`len(neighbors) == k`,
# meaning the index likely has more beyond this round) re-queries at `k *
# _OVERFETCH_FACTOR`, excluding ids already seen. Escalation stops at the
# first of `_MAX_OVERFETCH_ROUNDS` rounds or `k` reaching `_OVERFETCH_CAP`
# (whichever comes first) -- an intentionally small bound, not a promise to
# always find `limit` matches: a filter narrow enough to exhaust the cap
# without filling the result is still a documented limitation (README
# "Limitations"), just a much rarer one than the old fixed-4x single shot.
_OVERFETCH_FACTOR = 4
_MAX_OVERFETCH_ROUNDS = 3
_OVERFETCH_CAP = 1024

# Statuses `include_dead=False` drops. Archived/discarded are excluded
# unconditionally by `store.knn` itself (its join to `decisions`), regardless
# of `include_dead` -- they are never "dead history", they are gone.
_DEAD_STATUSES = frozenset({"superseded", "not_promoted"})


async def precedent(
    store: StorePort,
    embedder: Embedder,
    question: str,
    *,
    domains: Sequence[str] | None = None,
    tiers: Sequence[Tier] | None = None,
    limit: int = 10,
    include_dead: bool = True,
    compact_outcome_chars: int = 200,
) -> list[PrecedentHit]:
    """FR-6.3: nearest-precedent search for `question`.

    Pipeline: `embedder.embed([question])` -> `store.knn` (over-fetched when
    `domains`/`tiers`/`include_dead=False` will drop candidates, see
    `_OVERFETCH_FACTOR`; escalated across further rounds when a round still
    under-fills, see `_MAX_OVERFETCH_ROUNDS`/`_OVERFETCH_CAP`) ->
    `store.get_many_compact` (SQL-level projection, `outcome` truncated to
    `compact_outcome_chars` in SQL -- no full-row fetch then trim,
    docs/binnacle-core/components/04's "Compact projections are SQL-level" contract point)
    -> filter by `domains`/`tiers` if given -> drop superseded/not_promoted
    when `include_dead=False`, each survivor paired with its similarity.

    Archived/discarded decisions are never returned, regardless of
    `include_dead` (`store.knn` excludes them at the SQL join before this
    function ever sees a candidate). Superseded/not_promoted history IS
    returned by default (`include_dead=True`) -- labeled via
    `CompactDecision.status`, not hidden (FR-6.3).

    Ordering: `store.knn` already returns candidates similarity-descending;
    ties (and any candidate hydration reordering) are broken by `decision_id`
    for a fully deterministic result.

    Args:
        question: free-text question to embed and search against.
        domains: restrict results to these domains; `None` means all domains.
        tiers: restrict results to these tiers; `None` means both tiers.
        limit: maximum hits returned, after filtering.
        include_dead: include superseded/not_promoted history (default: yes).
        compact_outcome_chars: outcome truncation length for the hydrated
            projection (`BinnacleConfig.compact_outcome_chars`).

    Returns:
        Up to `limit` `PrecedentHit`s, similarity descending then id-ascending.
    """
    [vector] = await embedder.embed([question])

    needs_overfetch = bool(domains) or bool(tiers) or not include_dead
    k = limit * _OVERFETCH_FACTOR if needs_overfetch else limit
    k = min(k, _OVERFETCH_CAP)

    domain_set = set(domains) if domains else None
    tier_set = set(tiers) if tiers else None

    hits: list[PrecedentHit] = []
    seen_ids: list[UUID] = []
    for round_num in range(1, _MAX_OVERFETCH_ROUNDS + 1):
        neighbors = await store.knn(vector, k, exclude_ids=seen_ids)
        if not neighbors:
            break
        seen_ids.extend(decision_id for decision_id, _ in neighbors)

        ids = [decision_id for decision_id, _ in neighbors]
        compact_by_id = {
            c.id: c for c in await store.get_many_compact(ids, compact_chars=compact_outcome_chars)
        }

        for decision_id, similarity in neighbors:
            compact = compact_by_id.get(decision_id)
            if compact is None:
                continue
            if domain_set is not None and compact.domain not in domain_set:
                continue
            if tier_set is not None and compact.tier not in tier_set:
                continue
            if not include_dead and compact.status in _DEAD_STATUSES:
                continue
            hits.append(PrecedentHit(decision=compact, similarity=similarity))

        index_exhausted = len(neighbors) < k
        at_cap = k >= _OVERFETCH_CAP or round_num >= _MAX_OVERFETCH_ROUNDS
        if len(hits) >= limit or index_exhausted or at_cap:
            break
        k = min(k * _OVERFETCH_FACTOR, _OVERFETCH_CAP)

    hits.sort(key=lambda h: (-h.similarity, str(h.decision.id)))
    return hits[:limit]
