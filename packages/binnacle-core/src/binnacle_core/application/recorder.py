"""Shared "insert a new decision row" primitive (ARCHITECTURE.md §5, §6 package
layout — split from `lifecycle.py` because the same insert+refs+`recorded`
transition sequence is the entry point for FOUR distinct Lifecycle Engine acts:
`record`, `record_long_term`, `promote` (the long-term copy), and
`promote_refined` (the long-term copy). Keeping it here means every new decision
row in the system, regardless of which act minted it, gets identical treatment:
domain validated against the registry (FR-2.1), idempotent on `decision_id` +
content hash (FR-1.6), and a `recorded` transition whose `new_status` matches the
row's initial status so I-1's fold invariant holds from the row's very first
transition onward.
"""

from datetime import UTC, datetime
from uuid import uuid4

from binnacle_core.application.ports import InsertOutcome, StorePort, Tx
from binnacle_core.domain.errors import IdempotencyConflict, InactiveDomain, UnknownDomain
from binnacle_core.domain.models import Actor, Decision, LongStatus, NewDecision, ShortStatus, Tier


async def insert_new_decision(
    store: StorePort,
    tx: Tx,
    nd: NewDecision,
    actor: Actor,
    tier: Tier,
    status: ShortStatus | LongStatus,
) -> tuple[Decision, InsertOutcome]:
    """Validate `nd.domain`, mint an id if absent, insert the row + its refs, and
    append a `recorded` transition carrying `new_status=status`.

    Idempotent (FR-1.6): a retry with the same `decision_id` and identical content
    is a no-op — no refs/transition are written a second time, and the caller gets
    back the already-stored `Decision`.

    Returns:
        `(decision, 'inserted')` on a fresh row, `(existing, 'exists_identical')`
        on an idempotent retry.

    Raises:
        UnknownDomain: `nd.domain` is not a registered domain.
        InactiveDomain: `nd.domain` is registered but deactivated
            (`Binnacle.deactivate_domain`) — reactivate via `add_domain`
            (re-registering an existing name reactivates it) before recording.
        IdempotencyConflict: `nd.decision_id` exists with different content, or
            exists with identical content but a different tier.
    """
    active = await store.domain_active(tx, nd.domain)
    if active is None:
        msg = f"domain {nd.domain!r} is not registered in the domain registry"
        raise UnknownDomain(msg)
    if not active:
        msg = f"domain {nd.domain!r} is deactivated"
        raise InactiveDomain(msg)

    decision_id = nd.decision_id if nd.decision_id is not None else uuid4()
    decision = Decision(
        decision_id=decision_id,
        domain=nd.domain,
        tier=tier,
        status=status,
        scenario=nd.scenario,
        outcome=nd.outcome,
        reasoning=nd.reasoning,
        source=nd.source,
        recorded_by=actor,
        recorded_at=datetime.now(UTC),
        decided_at=nd.decided_at,
        options_considered=nd.options_considered,
        consequences=nd.consequences,
        confidence=nd.confidence,
        valid_from=nd.valid_from,
        valid_until=nd.valid_until,
        refs=nd.refs,
        supersedes=nd.supersedes,
        supplements=nd.supplements,
        metadata=nd.metadata,
    )
    outcome = await store.insert_decision(tx, decision, nd.content_hash())
    if outcome == "exists_identical":
        existing = await store.get_decision_tx(tx, decision_id)
        assert existing is not None, "insert_decision reported exists_identical for a missing row"
        if existing.tier != tier:
            # `content_hash` never covers `tier` (NewDecision.content_hash()'s
            # field list), so an id reused across a short-term `record` and a
            # long-term `record_long_term`/promotion with otherwise-identical
            # content would silently satisfy "exists_identical" against the
            # WRONG tier's row. Idempotency is about "the caller asked for this
            # exact decision again", and an id whose existing row is the wrong
            # tier is not that — treat it the same as any other divergent retry.
            msg = f"decision {decision_id} already recorded as tier {existing.tier!r}, not {tier!r}"
            raise IdempotencyConflict(msg)
        return existing, outcome

    await store.insert_refs(tx, decision_id, nd.refs)
    await store.apply_transition(tx, decision_id, "recorded", actor.as_str(), None, None, status)
    return decision, outcome
