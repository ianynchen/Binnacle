"""The backfill and discovery sweeps (docs/components/04-query-and-assist.md
"The sweeps"; REQUIREMENTS FR-6.9/FR-7.4). Free functions over `StorePort` +
the `Embedder`/`Suggester` ports (plus `LifecycleEngine` for the
promotion-recommendation half of `discover`), same shape as
`query.precedent` -- no state held between calls, each call is one
host-invoked, bounded, idempotent sweep.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from binnacle_core.application.lifecycle import LifecycleEngine
from binnacle_core.application.ports import Embedder, StorePort, Suggester
from binnacle_core.domain.errors import EmbeddingDimensionMismatch
from binnacle_core.domain.models import (
    Actor,
    BackfillSummary,
    CandidatePair,
    Decision,
    DiscoverySummary,
    QueueKind,
    Ref,
)

_ENGINE_ACTOR = Actor("engine", "binnacle")

# FR-6.9/OQ-3: the exact text embedded for a decision. `discover()` has no
# store primitive to read back an already-stored vector (StorePort exposes no
# `get_embedding`), so it re-derives a subject decision's vector for its own
# k-NN lookup by re-embedding this SAME text -- the join here is therefore the
# one place that must match between `backfill_embeddings` and `discover`.
_JOIN = "\n\n"


def _embedding_text(d: Decision) -> str:
    return _JOIN.join([d.scenario, d.outcome, d.reasoning])


# FR-7.4 taxonomy -> queue kind. 'unrelated' maps to nothing (no queue item).
_QUEUE_KIND: dict[str, QueueKind] = {
    "supersedes": "supersede",
    "supplements": "link",
    "conflicts": "conflict",
}


async def backfill_embeddings(
    store: StorePort, embedder: Embedder, embedding_dim: int, *, batch: int = 100
) -> BackfillSummary:
    """FR-6.9: embed up to `batch` decisions from the unembedded backlog and
    upsert their vectors.

    Every text in the batch is embedded in one `Embedder.embed` call, and
    every resulting vector's length is checked against `embedding_dim`
    BEFORE any `upsert_embedding` runs: a dimension mismatch is a config bug
    (the embedder and `BinnacleConfig.embedding_dim` disagree), so it aborts
    the whole batch with the backlog untouched rather than upserting some
    vectors and not others. Any other `embedder.embed` failure propagates the
    same way -- nothing has been upserted yet, so the backlog is intact for
    the next run (I-5: recall degrades, nothing breaks).

    Args:
        embedding_dim: the configured `BinnacleConfig.embedding_dim` every
            embedded vector must match.
        batch: maximum backlog decisions to process this call.

    Returns:
        `BackfillSummary` counting how many decisions were embedded; zero
        when the backlog is empty (idempotent no-op on a fully backfilled
        store -- a second consecutive call returns zero).

    Raises:
        EmbeddingDimensionMismatch: a returned vector's length does not
            match `embedding_dim`.
    """
    decisions = await store.unembedded(batch)
    if not decisions:
        return BackfillSummary(embedded=0)

    vectors = await embedder.embed([_embedding_text(d) for d in decisions])
    for vector in vectors:
        if len(vector) != embedding_dim:
            msg = f"embedder returned a {len(vector)}-dim vector, configured embedding_dim={embedding_dim}"
            raise EmbeddingDimensionMismatch(msg)

    async with store.transaction() as tx:
        for decision, vector in zip(decisions, vectors, strict=True):
            await store.upsert_embedding(tx, decision.decision_id, vector)
    return BackfillSummary(embedded=len(decisions))


def _subject_overlap(a: Sequence[Ref], b: Sequence[Ref]) -> bool:
    """FR-7.4's "subject overlap" structural filter, mirroring FR-6.1's own
    "subject-match OR unscoped" relevance rule: two decisions are related by
    subject when they share at least one identical (kind, identifier) subject
    ref, or when either side is unscoped (applies generally within its
    domain, so it can plausibly relate to anything)."""
    subjects_a = {(r.kind, r.identifier) for r in a if r.role == "subject"}
    subjects_b = {(r.kind, r.identifier) for r in b if r.role == "subject"}
    if not subjects_a or not subjects_b:
        return True
    return not subjects_a.isdisjoint(subjects_b)


def _structurally_related(subject: Decision, other: Decision) -> bool:
    """FR-7.4's structural filter (same domain, subject overlap, temporal
    order, status compatibility) applied to one k-NN candidate `other` of a
    newly discovered `subject`.

    - same domain: exact match.
    - subject overlap: `_subject_overlap`.
    - temporal order: `other` was recorded at or before `subject` -- a
      relationship suggestion always proposes the newer decision's stance on
      an earlier one, using `recorded_at` (always present) rather than the
      optional `decided_at`, matching every other sweep cursor's own
      oldest-first ordering. `recorded_at` is DB-clock-resolution, not
      globally unique, so a tie is broken by `decision_id`'s string form
      (smaller wins the "older" side) -- an arbitrary but total, deterministic
      order, chosen so exactly one of `(A, B)`/`(B, A)` ever survives.
      Without this, a tie lets `other` pass as `subject`'s elder AND
      `subject` pass as `other`'s elder in the SAME sweep (each is each
      other's own k-NN neighbor), producing a reversed-pair duplicate
      suggestion for every symmetric kind -- most visibly `conflicts`, since
      accepting both directions doubles the `CONFLICTS_WITH` link's
      `history().conflicts` entry.
    - status compatibility: both sides are still `current` -- a decision
      already superseded, promoted, or declined is no longer a meaningful
      target for a NEW relationship suggestion (archived/discarded are
      already excluded upstream by `store.knn`'s own join). This is also
      exactly the "both alive" rule a `conflicts` classification needs (a
      superseded or archived side is not a live conflict) -- the same filter
      serves every taxonomy kind, no `conflicts`-specific carve-out.
    """
    if subject.domain != other.domain:
        return False
    if subject.status != "current" or other.status != "current":
        return False
    other_key = (other.recorded_at, str(other.decision_id))
    subject_key = (subject.recorded_at, str(subject.decision_id))
    if other_key > subject_key:
        return False
    return _subject_overlap(subject.refs, other.refs)


async def _candidate_pairs(
    store: StorePort, embedder: Embedder, subject_id: UUID, k: int
) -> list[CandidatePair]:
    """One decision's O(k) candidate generation: re-embed its text -> k-NN ->
    structural filters -> hydrate compact projections for the survivors."""
    subject = await store.get_decision(subject_id)
    assert subject is not None, "undiscovered() returned an id with no decision row"

    [vector] = await embedder.embed([_embedding_text(subject)])
    neighbors = await store.knn(vector, k, exclude_ids=[subject_id])
    if not neighbors:
        return []

    others_by_id = {d.decision_id: d for d in await store.get_many([nid for nid, _ in neighbors])}
    survivors: list[tuple[UUID, float]] = []
    for nid, similarity in neighbors:
        other = others_by_id.get(nid)
        if other is not None and _structurally_related(subject, other):
            survivors.append((nid, similarity))
    if not survivors:
        return []

    compact_ids = [subject_id, *(nid for nid, _ in survivors)]
    compact_by_id = {c.id: c for c in await store.get_many_compact(compact_ids)}
    subject_compact = compact_by_id.get(subject_id)
    if subject_compact is None:
        return []

    pairs: list[CandidatePair] = []
    for nid, similarity in survivors:
        other_compact = compact_by_id.get(nid)
        if other_compact is not None:
            pairs.append(
                CandidatePair(decision=subject_compact, other=other_compact, similarity=similarity)
            )
    return pairs


async def _recommend_aging(
    store: StorePort,
    suggester: Suggester,
    engine: LifecycleEngine,
    archival_age_days: int,
    batch: int,
) -> int:
    """FR-7.2's promotion-candidate sweep: `Suggester.assess_promotion` over
    short-term `current` decisions aging without a recommendation. Positive
    assessments go through `LifecycleEngine.recommend` -- never a direct
    `enqueue` -- so the recommendation is also transition-logged, the same
    audited path a human or agent recommender uses (FR-4.2); `recommend`'s
    own structural dedup (an already-open `promote` item) makes re-running
    this safe.

    The aging window is `archival_age_days / 2`: not separately specified by
    04/REQUIREMENTS (ruling recorded in the Task 8 report), chosen so a
    decision idle long enough to be a promotion candidate surfaces for review
    well before it is old enough to be auto-archived out from under it.
    """
    cutoff = datetime.now(UTC) - timedelta(days=archival_age_days / 2)
    aging = await store.aging_unrecommended(cutoff, batch)
    if not aging:
        return 0
    assessments = await suggester.assess_promotion(aging)
    recommended = 0
    for assessment in assessments:
        if not assessment.recommend:
            continue
        await engine.recommend(assessment.decision_id, _ENGINE_ACTOR, assessment.rationale)
        recommended += 1
    return recommended


async def discover(
    store: StorePort,
    embedder: Embedder,
    suggester: Suggester | None,
    engine: LifecycleEngine,
    *,
    k: int,
    confidence_floor: float,
    per_sweep_cap: int,
    archival_age_days: int,
    batch: int = 100,
) -> DiscoverySummary:
    """FR-7.4 discovery sweep: cursor-driven relationship discovery over
    embeddings where `discovered_at IS NULL`, plus FR-7.2's promotion-
    candidate sweep over aging unrecommended decisions.

    Per decision from `store.undiscovered(batch)`: `_candidate_pairs` (O(k)
    k-NN + structural filters) -> `suggester.classify_pairs` (skipped
    entirely when there are no surviving candidates) -> for each
    (pair, suggestion): 'unrelated' is dropped, below-`confidence_floor` is
    dropped and counted, otherwise `store.enqueue` (kind 'supersede' for
    'supersedes', 'link' for 'supplements', 'conflict' for 'conflicts') -- a
    `None` return (an identical open item already exists) is tolerated and
    counted as deduped rather than
    treated as an error. `store.mark_discovered` runs in the SAME transaction
    as that decision's enqueues, only once every one of its classified pairs
    has been considered -- a process death between `classify_pairs` (not
    itself transactional) and that transaction committing leaves
    `discovered_at` NULL, so the next sweep re-discovers the same decision
    from scratch (enqueue's dedup makes that safe).

    `per_sweep_cap` bounds the total relationship items enqueued THIS call:
    once reached, no further decision is even started (it and everything
    after it in this batch stay `discovered_at IS NULL` for the next sweep),
    and if it is reached partway through one decision's own pairs, that
    decision's already-committed partial enqueues stand but it is likewise
    left un-marked. `per_sweep_cap`/`confidence_floor` apply only to this
    relationship-suggestion half (04's "queue items with rationale+
    confidence, floor-filtered, per-sweep capped" sentence); the promotion-
    candidate half (`_recommend_aging`) has no floor/cap of its own -- FR-7.2
    states plainly "for positive assessments", not a confidence-gated set.

    Returns:
        `DiscoverySummary`, all zero when `suggester is None` (FR-7.4: "No
        Suggester configured -> sweep no-ops cleanly" -- neither cursor is
        even read) or when both cursors are empty.
    """
    if suggester is None:
        return DiscoverySummary(0, 0, 0, 0, 0)

    decisions_processed = 0
    suggestions_enqueued = 0
    suggestions_deduped = 0
    suggestions_below_floor = 0
    enqueued_this_sweep = 0

    for subject_id in await store.undiscovered(batch):
        if enqueued_this_sweep >= per_sweep_cap:
            break

        pairs = await _candidate_pairs(store, embedder, subject_id, k)
        suggestions = await suggester.classify_pairs(pairs) if pairs else []

        fully_processed = True
        async with store.transaction() as tx:
            for pair, suggestion in zip(pairs, suggestions, strict=True):
                kind = _QUEUE_KIND.get(suggestion.kind)
                if kind is None:
                    continue
                if suggestion.confidence < confidence_floor:
                    suggestions_below_floor += 1
                    continue
                if enqueued_this_sweep >= per_sweep_cap:
                    fully_processed = False
                    break
                item_id = await store.enqueue(
                    tx,
                    kind,
                    pair.decision.id,
                    pair.other.id,
                    _ENGINE_ACTOR,
                    suggestion.rationale,
                    suggestion.confidence,
                )
                if item_id is None:
                    suggestions_deduped += 1
                else:
                    suggestions_enqueued += 1
                    enqueued_this_sweep += 1
            if fully_processed:
                await store.mark_discovered(tx, [subject_id])
        if fully_processed:
            decisions_processed += 1

    promotions_recommended = await _recommend_aging(
        store, suggester, engine, archival_age_days, batch
    )

    return DiscoverySummary(
        decisions_processed=decisions_processed,
        suggestions_enqueued=suggestions_enqueued,
        suggestions_deduped=suggestions_deduped,
        suggestions_below_floor=suggestions_below_floor,
        promotions_recommended=promotions_recommended,
    )
