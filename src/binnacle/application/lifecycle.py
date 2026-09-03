"""The Lifecycle Engine (docs/components/03-lifecycle-engine.md; REQUIREMENTS
FR-3/4/5; ARCHITECTURE.md I-1..I-4). The ONLY writer of `decisions.status`,
`links`, and `transitions` — every act below is exactly one `store.transaction()`,
opening with `lock_decisions` on every row it touches (I-1: concurrent acts
serialize on that lock, so a validate-then-write race cannot land two
inconsistent outcomes) and ending with the transition(s) that make `status` the
computable fold of the transition log.

Authority (I-2) is checked before any transaction opens wherever it can be
decided from data that never changes after insert (`tier`, `recorded_by`) — both
are read once via a plain `get_decision` peek, which is race-free precisely
because neither column is ever written again. Checks that depend on the row's
current `status` (which DOES mutate) happen after the row is locked, inside the
transaction, so a state that changed between the peek and the lock is caught by
the post-lock legality check rather than trusted from the stale peek.

Two shared act-internal helpers do the repeated wiring:

- `_execute_supersede` / `_execute_supplement`: link + the two-sided transition
  pair, reused by `supersede`/`supplement` (direct calls), `record`'s inline
  short-term supersede, `apply_item` (executing a suggested link/supersede), and
  `promote`/`promote_refined`'s pending-claim execution.
- `_void_open_items`: FR-4.3's "leaves current outside the gate voids its open
  queue items" rule, reused by `discard`, `supersede` (on the target), and
  `promote_refined` (on each source, after that source's own pending claims have
  already been executed and so are no longer open).
"""

from collections.abc import Sequence
from uuid import UUID

from binnacle.application.ports import DecisionRow, StorePort, Tx
from binnacle.application.recorder import insert_new_decision
from binnacle.domain.errors import AuthorityViolation, DecisionNotFound, InvalidTransition
from binnacle.domain.models import Actor, Decision, NewDecision, QueueItemView

# ST statuses from which a decision may be superseded (03's exit matrix).
_ST_SUPERSEDABLE = frozenset({"current", "not_promoted"})
# LT has only current|superseded; only current may be superseded (FR-5.2a).
_LT_SUPERSEDABLE = frozenset({"current"})
# ST statuses that are still "in play": eligible for the promotion gate
# (promote/promote_refined/decline — recommend re-opens it for a declined
# not_promoted decision too, FR-4.5) and for archival.
_ST_ACTIVE = frozenset({"current", "not_promoted"})
# ST statuses from which discard is reachable at all, before the FR-3.3
# actor-specific narrowing below.
_ST_DISCARDABLE = frozenset({"current", "not_promoted", "archived"})


class LifecycleEngine:
    """FR-3/4/5's invariant-bearing core. See module docstring."""

    def __init__(self, store: StorePort) -> None:
        self._store = store

    # -- recording (FR-1, FR-4.4) ------------------------------------------

    async def record(self, nd: NewDecision, actor: Actor) -> Decision:
        """Record `nd` into the short-term tier (any actor).

        A declared `supersedes` target that is itself short-term is linked and
        superseded inline, in the same transaction (FR-5.2: short-term ↔
        short-term is ungated). A long-term target instead files a pending
        `queue(kind='supersede')` claim — only a promoting human can execute a
        long-term mutation (I-2), so the claim waits for the gate.

        Raises:
            UnknownDomain: `nd.domain` is not registered.
            IdempotencyConflict: `nd.decision_id` exists with different content.
            DecisionNotFound: a declared `supersedes` target does not exist.
            InvalidTransition: a declared short-term `supersedes` target is not
                supersedable (wrong status, or would create a cycle).
        """
        async with self._store.transaction() as tx:
            decision, outcome = await insert_new_decision(
                self._store, tx, nd, actor, "short_term", "current"
            )
            if outcome == "exists_identical":
                return decision
            for target_id in nd.supersedes:
                await self._link_declared_supersede(tx, decision.decision_id, target_id, actor)
        return decision

    async def record_long_term(self, nd: NewDecision, actor: Actor) -> Decision:
        """Record `nd` directly into the long-term tier (FR-4.4, human only):
        one transaction, `recorded` + `promoted` transitions, `status='current'`.

        Raises:
            AuthorityViolation: `actor.kind != 'human'`.
            UnknownDomain: `nd.domain` is not registered.
            IdempotencyConflict: `nd.decision_id` exists with different content.
        """
        if actor.kind != "human":
            msg = "record_long_term requires a human actor"
            raise AuthorityViolation(msg)
        async with self._store.transaction() as tx:
            decision, outcome = await insert_new_decision(
                self._store, tx, nd, actor, "long_term", "current"
            )
            if outcome == "exists_identical":
                return decision
            await self._store.apply_transition(
                tx, decision.decision_id, "promoted", actor.as_str(), None, None, None
            )
        return decision

    # -- the review gate (FR-4) ----------------------------------------------

    async def recommend(self, decision_id: UUID, actor: Actor, reason: str | None) -> int | None:
        """File a promotion recommendation (any actor) on a short-term decision.

        A decision that is currently `archived` is implicitly reactivated first —
        both transitions land in this one transaction (FR-3.4: re-recommendation
        by any actor reactivates, regardless of who recorded it).

        Returns:
            The new queue item's id, or `None` when an identical open item
            already exists (structural dedup — the recommendation still landed
            in the transition log, just not as a second queue row).

        Raises:
            DecisionNotFound: `decision_id` does not exist.
            InvalidTransition: the decision is long-term, or is in a terminal
                short-term status (`promoted`/`superseded`/`discarded`).
        """
        async with self._store.transaction() as tx:
            row = await self._lock_one(tx, decision_id)
            if row.tier != "short_term":
                msg = "only short-term decisions can be recommended"
                raise InvalidTransition(row.status, "recommend", msg)
            if row.status == "archived":
                await self._reactivate_locked(tx, decision_id, actor)
            elif row.status not in _ST_ACTIVE:
                msg = "decision is in a terminal status"
                raise InvalidTransition(row.status, "recommend", msg)
            item_id = await self._store.enqueue(
                tx, "promote", decision_id, None, actor, reason, None
            )
            await self._store.apply_transition(
                tx, decision_id, "recommended", actor.as_str(), reason, None, None
            )
        return item_id

    async def promote(self, item_id: int, actor: Actor) -> Decision:
        """Execute a pending promotion (human only): verbatim long-term copy +
        `PROMOTED_FROM` link + the source's own pending long-term-supersede
        claims (FR-5.2a: their `SUPERSEDES` link's `from` is this new copy, never
        the short-term source) + source → `promoted` + queue resolution.

        Raises:
            AuthorityViolation: `actor.kind != 'human'`.
            ItemNotFound: `item_id` does not exist.
            ItemAlreadyResolved: `item_id` was already resolved.
            InvalidTransition: the item is not a `promote` item, or the source
                is not eligible (wrong tier or status).
        """
        if actor.kind != "human":
            msg = "promote requires a human actor"
            raise AuthorityViolation(msg)
        peeked = await self._peek_item(item_id)
        async with self._store.transaction() as tx:
            if peeked is not None:
                await self._lock_one(tx, peeked.item.decision_id)
            item = await self._store.resolve_item(tx, item_id)
            if item.kind != "promote":
                msg = "queue item is not a pending promotion"
                raise InvalidTransition(item.kind, "promote", msg)
            source_id = item.decision_id
            row = await self._lock_one(tx, source_id)
            if row.tier != "short_term" or row.status not in _ST_ACTIVE:
                msg = "source is not eligible for promotion"
                raise InvalidTransition(row.status, "promote", msg)

            source = await self._store.get_decision(source_id)
            assert source is not None, "locked row disappeared mid-transaction"
            lt_copy, _ = await insert_new_decision(
                self._store, tx, _verbatim_copy(source), actor, "long_term", "current"
            )
            await self._store.insert_link(tx, lt_copy.decision_id, source_id, "PROMOTED_FROM")
            await self._store.apply_transition(
                tx,
                source_id,
                "promoted",
                actor.as_str(),
                None,
                {"item_id": item_id, "target": str(lt_copy.decision_id)},
                "promoted",
            )
            await self._execute_pending_claims(tx, source_id, lt_copy.decision_id, actor)
        return lt_copy

    async def promote_refined(
        self, source_ids: Sequence[UUID], refined: NewDecision, actor: Actor
    ) -> Decision:
        """Consolidate one or more short-term sources into one human-authored
        long-term decision (FR-4.6): `refined` is validated like any recording,
        forced to `tier='long_term'`, `recorded_by=actor`. Every source gets a
        `PROMOTED_FROM` link and a `promoted` transition (`payload.refined =
        True`), its pending long-term-supersede claims execute against the new
        copy, and any of its other open queue items are voided.

        Raises:
            AuthorityViolation: `actor.kind != 'human'`.
            ValueError: `source_ids` is empty.
            DecisionNotFound: a source does not exist.
            UnknownDomain: `refined.domain` is not registered.
            InvalidTransition: a source is not eligible (wrong tier or status).
        """
        if actor.kind != "human":
            msg = "promote_refined requires a human actor"
            raise AuthorityViolation(msg)
        ids = list(source_ids)
        if not ids:
            msg = "promote_refined requires at least one source"
            raise ValueError(msg)
        async with self._store.transaction() as tx:
            locked = await self._store.lock_decisions(tx, ids)
            for source_id in ids:
                row = _require(locked, source_id)
                if row.tier != "short_term" or row.status not in _ST_ACTIVE:
                    msg = "source is not eligible for promotion"
                    raise InvalidTransition(row.status, "promote_refined", msg)

            lt_copy, _ = await insert_new_decision(
                self._store, tx, refined, actor, "long_term", "current"
            )
            for source_id in ids:
                await self._store.insert_link(tx, lt_copy.decision_id, source_id, "PROMOTED_FROM")
                await self._store.apply_transition(
                    tx,
                    source_id,
                    "promoted",
                    actor.as_str(),
                    None,
                    {"refined": True, "target": str(lt_copy.decision_id)},
                    "promoted",
                )
                await self._execute_pending_claims(tx, source_id, lt_copy.decision_id, actor)
                await self._void_open_items(tx, source_id, actor)
        return lt_copy

    async def decline(self, item_id: int, actor: Actor, reason: str | None) -> None:
        """Decline a pending promotion (human only): source → `not_promoted`.
        Not terminal (FR-4.5) — the decision may be re-recommended later.

        Raises:
            AuthorityViolation: `actor.kind != 'human'`.
            ItemNotFound: `item_id` does not exist.
            ItemAlreadyResolved: `item_id` was already resolved.
            InvalidTransition: the item is not a `promote` item, or the source
                is not eligible (wrong tier or status).
        """
        if actor.kind != "human":
            msg = "decline requires a human actor"
            raise AuthorityViolation(msg)
        peeked = await self._peek_item(item_id)
        async with self._store.transaction() as tx:
            if peeked is not None:
                await self._lock_one(tx, peeked.item.decision_id)
            item = await self._store.resolve_item(tx, item_id)
            if item.kind != "promote":
                msg = "queue item is not a pending promotion"
                raise InvalidTransition(item.kind, "decline", msg)
            source_id = item.decision_id
            row = await self._lock_one(tx, source_id)
            if row.tier != "short_term" or row.status not in _ST_ACTIVE:
                msg = "source is not eligible for decline"
                raise InvalidTransition(row.status, "decline", msg)
            await self._store.apply_transition(
                tx,
                source_id,
                "declined",
                actor.as_str(),
                reason,
                {"item_id": item_id},
                "not_promoted",
            )

    # -- direct status acts (FR-3) -------------------------------------------

    async def discard(self, decision_id: UUID, actor: Actor, reason: str | None) -> None:
        """Discard a short-term decision (FR-3.3): the recording actor for its
        own `current` decision, or a human for any short-term decision. Auto-voids
        the decision's open queue items (FR-4.3).

        Raises:
            DecisionNotFound: `decision_id` does not exist.
            AuthorityViolation: a non-human actor other than the recorder, or the
                recorder attempting to discard a decision that is no longer
                `current`.
            InvalidTransition: the decision is long-term, or is in a terminal
                short-term status.
        """
        peek = await self._store.get_decision(decision_id)
        if peek is None:
            raise DecisionNotFound(str(decision_id))
        if actor.kind != "human" and actor != peek.recorded_by:
            msg = "discard requires the recording actor or a human"
            raise AuthorityViolation(msg)
        async with self._store.transaction() as tx:
            row = await self._lock_one(tx, decision_id)
            if row.tier != "short_term":
                msg = "long-term decisions cannot be discarded"
                raise InvalidTransition(row.status, "discard", msg)
            if row.status not in _ST_DISCARDABLE:
                msg = "decision is in a terminal status"
                raise InvalidTransition(row.status, "discard", msg)
            if actor.kind != "human" and row.status != "current":
                msg = "the recording actor may only discard its own current decision"
                raise AuthorityViolation(msg)
            await self._store.apply_transition(
                tx, decision_id, "discarded", actor.as_str(), reason, None, "discarded"
            )
            await self._void_open_items(tx, decision_id, actor)

    async def supersede(self, new_id: UUID, old_id: UUID, actor: Actor) -> None:
        """Link `new_id` as superseding `old_id` (FR-5.2a tier symmetry): ungated
        short-term ↔ short-term, but a long-term `old_id` requires a human actor
        AND a long-term `new_id`. Auto-voids `old_id`'s open queue items.

        Raises:
            DecisionNotFound: either id does not exist.
            AuthorityViolation: `old_id` is long-term and `actor.kind != 'human'`.
            InvalidTransition: the tiers don't match, `old_id` is not
                supersedable, or the link would create a cycle.
        """
        old_peek = await self._store.get_decision(old_id)
        if old_peek is None:
            raise DecisionNotFound(str(old_id))
        if old_peek.tier == "long_term" and actor.kind != "human":
            msg = "superseding a long-term decision requires a human actor"
            raise AuthorityViolation(msg)
        async with self._store.transaction() as tx:
            locked = await self._store.lock_decisions(tx, [new_id, old_id])
            old_row = _require(locked, old_id)
            new_row = _require(locked, new_id)
            self._validate_supersede(old_row, new_row, "supersede")
            await self._check_acyclic(old_id, new_id, old_row.status, "supersede")
            await self._execute_supersede(tx, new_id, old_id, actor, item_id=None)
            await self._void_open_items(tx, old_id, actor)

    async def supplement(self, new_id: UUID, old_id: UUID, actor: Actor) -> None:
        """Link `new_id` as supplementing `old_id` (FR-5.3): no status change on
        either side. A long-term `old_id` requires a human actor (I-2); no tier
        symmetry is required (unlike `supersede`, FR-5.2a).

        Raises:
            DecisionNotFound: either id does not exist.
            AuthorityViolation: `old_id` is long-term and `actor.kind != 'human'`.
        """
        old_peek = await self._store.get_decision(old_id)
        if old_peek is None:
            raise DecisionNotFound(str(old_id))
        if old_peek.tier == "long_term" and actor.kind != "human":
            msg = "supplementing a long-term decision requires a human actor"
            raise AuthorityViolation(msg)
        async with self._store.transaction() as tx:
            locked = await self._store.lock_decisions(tx, [new_id, old_id])
            _require(locked, old_id)
            _require(locked, new_id)
            await self._execute_supplement(tx, new_id, old_id, actor, item_id=None)

    async def reactivate(self, decision_id: UUID, actor: Actor) -> None:
        """Reactivate an archived decision (any actor — FR-3.4: harmless), restoring
        the status it held immediately before archival.

        Raises:
            DecisionNotFound: `decision_id` does not exist.
            InvalidTransition: the decision is not currently `archived`.
        """
        async with self._store.transaction() as tx:
            row = await self._lock_one(tx, decision_id)
            if row.status != "archived":
                msg = "decision is not archived"
                raise InvalidTransition(row.status, "reactivate", msg)
            await self._reactivate_locked(tx, decision_id, actor)

    async def archive(self, decision_ids: Sequence[UUID], actor: Actor) -> int:
        """Archive every id in `decision_ids` (engine or human — the sweep
        attests `Actor('engine', 'binnacle')`), atomically: either all archive or
        none do. Direct calls refuse outright when any id is ineligible (the
        archival sweep is expected to pre-filter via `archival_eligible` instead
        of relying on this method to skip ineligible ids).

        Returns:
            The number of decisions archived (always `len(decision_ids)` — a
            partial failure raises instead of partially archiving).

        Raises:
            AuthorityViolation: `actor.kind not in ('engine', 'human')`.
            DecisionNotFound: an id does not exist.
            InvalidTransition: an id is long-term, in a non-archivable status, or
                has open queue items.
        """
        if actor.kind not in ("engine", "human"):
            msg = "archive requires an engine or human actor"
            raise AuthorityViolation(msg)
        ids = list(decision_ids)
        if not ids:
            return 0
        async with self._store.transaction() as tx:
            locked = await self._store.lock_decisions(tx, ids)
            for decision_id in ids:
                row = _require(locked, decision_id)
                if row.tier != "short_term" or row.status not in _ST_ACTIVE:
                    msg = "decision is not archivable"
                    raise InvalidTransition(row.status, "archive", msg)
                if await self._store.open_items_for(tx, decision_id):
                    msg = "decision has open queue items"
                    raise InvalidTransition(row.status, "archive", msg)
            for decision_id in ids:
                await self._store.apply_transition(
                    tx, decision_id, "archived", actor.as_str(), None, None, "archived"
                )
        return len(ids)

    # -- queue resolution (FR-4.3) -------------------------------------------

    async def apply_item(self, item_id: int, actor: Actor) -> None:
        """Execute a suggested `link` (SUPPLEMENTS) or `supersede` item (human
        only, always — a suggestion may touch a long-term decision, I-2).

        Raises:
            AuthorityViolation: `actor.kind != 'human'`.
            ItemNotFound: `item_id` does not exist.
            ItemAlreadyResolved: `item_id` was already resolved.
            InvalidTransition: the item is not a `link`/`supersede` item, its
                target is missing, or (for `supersede`) the tiers don't match,
                the target isn't supersedable, or it would create a cycle.
            DecisionNotFound: the item's decision or target does not exist.
        """
        if actor.kind != "human":
            msg = "apply_item requires a human actor"
            raise AuthorityViolation(msg)
        peeked = await self._peek_item(item_id)
        async with self._store.transaction() as tx:
            if peeked is not None:
                ids = [peeked.item.decision_id]
                if peeked.item.target_id is not None:
                    ids.append(peeked.item.target_id)
                await self._store.lock_decisions(tx, ids)
            item = await self._store.resolve_item(tx, item_id)
            if item.kind not in ("link", "supersede") or item.target_id is None:
                msg = "queue item is not an applicable link/supersede suggestion"
                raise InvalidTransition(item.kind, "apply_item", msg)
            new_id, old_id = item.decision_id, item.target_id
            locked = await self._store.lock_decisions(tx, [new_id, old_id])
            old_row = _require(locked, old_id)
            new_row = _require(locked, new_id)
            if item.kind == "supersede":
                self._validate_supersede(old_row, new_row, "apply_item")
                await self._check_acyclic(old_id, new_id, old_row.status, "apply_item")
                await self._execute_supersede(tx, new_id, old_id, actor, item_id=item_id)
            else:
                await self._execute_supplement(tx, new_id, old_id, actor, item_id=item_id)

    async def dismiss_item(self, item_id: int, actor: Actor, reason: str | None) -> None:
        """Dismiss a queue item (human only) as noise: resolves the item and logs
        a `dismissed` transition on its decision. The decision itself is
        untouched — no status change, no link.

        Raises:
            AuthorityViolation: `actor.kind != 'human'`.
            ItemNotFound: `item_id` does not exist.
            ItemAlreadyResolved: `item_id` was already resolved.
        """
        if actor.kind != "human":
            msg = "dismiss_item requires a human actor"
            raise AuthorityViolation(msg)
        peeked = await self._peek_item(item_id)
        async with self._store.transaction() as tx:
            if peeked is not None:
                await self._lock_one(tx, peeked.item.decision_id)
            item = await self._store.resolve_item(tx, item_id)
            await self._lock_one(tx, item.decision_id)
            await self._store.apply_transition(
                tx,
                item.decision_id,
                "dismissed",
                actor.as_str(),
                reason,
                {"item_id": item_id},
                None,
            )

    # -- internal helpers -----------------------------------------------------

    async def _lock_one(self, tx: Tx, decision_id: UUID) -> DecisionRow:
        locked = await self._store.lock_decisions(tx, [decision_id])
        return _require(locked, decision_id)

    async def _peek_item(self, item_id: int) -> QueueItemView | None:
        """A lock-free, best-effort look at an open queue item's (immutable)
        `decision_id`/`target_id`/`kind`, read BEFORE opening a transaction.

        Exists to keep lock acquisition order consistent across the engine:
        decision-first acts (`supersede`/`discard`/`promote_refined`) lock their
        decision(s) before touching a queue row (in `_void_open_items`), while
        item-first acts (`promote`/`decline`/`apply_item`/`dismiss_item`) only
        learn their decision id by resolving the item. Without this peek, those
        two groups would acquire the decision-row and queue-row locks in opposite
        orders, which is exactly the shape of a deadlock: item-first act A holds
        the queue-row lock waiting for the decision-row lock supersede-family act
        B already holds, while B's own `_void_open_items` waits on the same
        queue-row lock A holds. Peeking here (a plain read, no lock) lets the
        item-first acts take their decision lock(s) FIRST too, restoring one
        global order (decisions before queue rows) everywhere.

        Returns `None` when the item isn't currently open (already resolved, or
        never existed, or — vanishingly rarely — was enqueued after this peek
        ran); callers fall back to `resolve_item` alone in that case, which still
        raises the correct `ItemNotFound`/`ItemAlreadyResolved`.
        """
        for view in await self._store.open_queue():
            if view.item.item_id == item_id:
                return view
        return None

    async def _restored_status(self, decision_id: UUID) -> str:
        """The status recorded immediately before the transition that most
        recently set `new_status='archived'` — computed from the transition log,
        which the acyclicity of the fold (I-1) guarantees is well-defined once a
        decision has actually been archived."""
        history = await self._store.history(decision_id)
        non_null = [t.new_status for t in history.transitions if t.new_status is not None]
        return non_null[-2]

    async def _reactivate_locked(self, tx: Tx, decision_id: UUID, actor: Actor) -> None:
        """Write the `reactivated` transition for an already-locked, already
        confirmed-`archived` decision. Factored out so `recommend`'s implicit
        reactivation and `reactivate` itself share one implementation."""
        restored = await self._restored_status(decision_id)
        await self._store.apply_transition(
            tx, decision_id, "reactivated", actor.as_str(), None, None, restored
        )

    def _validate_supersede(self, old_row: DecisionRow, new_row: DecisionRow, action: str) -> None:
        if new_row.tier != old_row.tier:
            msg = f"tier mismatch: old is {old_row.tier}, new is {new_row.tier}"
            raise InvalidTransition(old_row.status, action, msg)
        legal = _ST_SUPERSEDABLE if old_row.tier == "short_term" else _LT_SUPERSEDABLE
        if old_row.status not in legal:
            msg = "old decision is not in a supersedable status"
            raise InvalidTransition(old_row.status, action, msg)

    async def _check_acyclic(
        self, old_id: UUID, new_id: UUID, old_status: str, action: str
    ) -> None:
        """Raise if linking `new_id` as `old_id`'s successor would close a cycle:
        true exactly when `old_id` already (transitively) supersedes `new_id` —
        i.e. `new_id` is already one of `old_id`'s predecessors."""
        history = await self._store.history(old_id)
        if new_id in {d.decision_id for d in history.predecessors}:
            msg = "would create a supersession cycle"
            raise InvalidTransition(old_status, action, msg)

    async def _execute_supersede(
        self, tx: Tx, new_id: UUID, old_id: UUID, actor: Actor, item_id: int | None
    ) -> None:
        """Link + the two-sided `superseded` transition pair. Does NOT resolve
        `item_id` — callers that reached this via a queue item resolve it
        themselves (either before, via `resolve_item`'s read-and-guard, or
        explicitly), so this stays a pure "apply the effect" step."""
        payload_old: dict[str, object] = {"target": str(new_id)}
        payload_new: dict[str, object] = {"target": str(old_id)}
        if item_id is not None:
            payload_old["item_id"] = item_id
            payload_new["item_id"] = item_id
        await self._store.insert_link(tx, new_id, old_id, "SUPERSEDES")
        await self._store.apply_transition(
            tx, old_id, "superseded", actor.as_str(), None, payload_old, "superseded"
        )
        await self._store.apply_transition(
            tx, new_id, "superseded", actor.as_str(), None, payload_new, None
        )

    async def _execute_supplement(
        self, tx: Tx, new_id: UUID, old_id: UUID, actor: Actor, item_id: int | None
    ) -> None:
        """Link + the two-sided `supplement_linked` transition pair. No status
        change on either side (FR-5.3)."""
        payload_old: dict[str, object] = {"target": str(new_id)}
        payload_new: dict[str, object] = {"target": str(old_id)}
        if item_id is not None:
            payload_old["item_id"] = item_id
            payload_new["item_id"] = item_id
        await self._store.insert_link(tx, new_id, old_id, "SUPPLEMENTS")
        await self._store.apply_transition(
            tx, old_id, "supplement_linked", actor.as_str(), None, payload_old, None
        )
        await self._store.apply_transition(
            tx, new_id, "supplement_linked", actor.as_str(), None, payload_new, None
        )

    async def _void_open_items(self, tx: Tx, decision_id: UUID, actor: Actor) -> None:
        """FR-4.3: every still-open queue item belonging to `decision_id` is
        resolved and logged as `voided` — called whenever a decision leaves
        `current` outside the gate (or, for `promote_refined`, after that
        source's OWN pending claims have already been executed and so are no
        longer open to void)."""
        for item in await self._store.open_items_for(tx, decision_id):
            await self._store.resolve_item(tx, item.item_id)
            await self._store.apply_transition(
                tx, decision_id, "voided", actor.as_str(), None, {"item_id": item.item_id}, None
            )

    async def _execute_pending_claims(
        self, tx: Tx, source_id: UUID, lt_copy_id: UUID, actor: Actor
    ) -> None:
        """FR-5.2a: execute every open `queue(kind='supersede')` claim `source_id`
        filed at record time against a long-term target — the SUPERSEDES link's
        `from` is `lt_copy_id`, never `source_id` itself, since a short-term
        decision may never directly supersede a long-term one. No acyclicity
        check is needed: `lt_copy_id` is a freshly minted id with no existing
        links, so it cannot already be an ancestor of anything.
        """
        for item in await self._store.open_items_for(tx, source_id):
            if item.kind != "supersede":
                continue
            target_id = item.target_id
            assert target_id is not None, "a supersede claim always carries a target"
            locked = await self._store.lock_decisions(tx, [target_id])
            target_row = _require(locked, target_id)
            if target_row.tier != "long_term" or target_row.status not in _LT_SUPERSEDABLE:
                msg = "pending supersede claim's target is no longer supersedable"
                raise InvalidTransition(target_row.status, "promote", msg)
            await self._execute_supersede(tx, lt_copy_id, target_id, actor, item_id=item.item_id)
            await self._store.resolve_item(tx, item.item_id)

    async def _link_declared_supersede(
        self, tx: Tx, new_id: UUID, target_id: UUID, actor: Actor
    ) -> None:
        """`record`'s declared-`supersedes` handling: a short-term target is
        superseded inline (ungated, FR-5.2); a long-term target instead files a
        pending claim (I-2) that only a promoting human can later execute."""
        locked = await self._store.lock_decisions(tx, [target_id])
        target_row = _require(locked, target_id)
        if target_row.tier == "long_term":
            await self._store.enqueue(tx, "supersede", new_id, target_id, actor, None, None)
            return
        if target_row.status not in _ST_SUPERSEDABLE:
            msg = "declared supersede target is not in a supersedable status"
            raise InvalidTransition(target_row.status, "supersede", msg)
        await self._check_acyclic(target_id, new_id, target_row.status, "supersede")
        await self._execute_supersede(tx, new_id, target_id, actor, item_id=None)


def _require(locked: dict[UUID, DecisionRow], decision_id: UUID) -> DecisionRow:
    row = locked.get(decision_id)
    if row is None:
        raise DecisionNotFound(str(decision_id))
    return row


def _verbatim_copy(source: Decision) -> NewDecision:
    """A `NewDecision` carrying `source`'s content, for `promote`'s long-term
    copy — no `decision_id` (a fresh id is always minted for the copy), and no
    `supersedes`/`supplements` (those are link-table facts, not re-declared)."""
    return NewDecision(
        domain=source.domain,
        scenario=source.scenario,
        outcome=source.outcome,
        reasoning=source.reasoning,
        source=source.source,
        options_considered=source.options_considered,
        consequences=source.consequences,
        confidence=source.confidence,
        decided_at=source.decided_at,
        valid_from=source.valid_from,
        valid_until=source.valid_until,
        refs=source.refs,
        metadata=source.metadata,
    )
