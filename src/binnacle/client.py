"""The `Binnacle` client (docs/components/01-configuration-and-client.md): the
public face every caller — meridian's UI/API/MCP surface, its sweep jobs, its
agent tools — programs against. Everything else in the package is reachable
only through this surface (FR-8.1: library, not authority).

Layering note (deviation from the plan's file list, controller-pre-approved):
the plan names `application/client.py`, but `.importlinter`'s `layers`
contract is `binnacle.adapters -> binnacle.application -> binnacle.domain`
(adapters may import application+domain; application may import only domain).
Building the store means constructing `PostgresStore`
(`binnacle.adapters.postgres_store`) — `application` importing `adapters`
would invert that contract and fail `lint-imports`. This module lives at the
package top level instead, which the layers contract does not constrain, so it
may import both `application` (for `LifecycleEngine`, ports, ..) and
`adapters` (for `PostgresStore`) while `application` itself stays
adapter-free.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal
from uuid import UUID

from binnacle.adapters.postgres_store import PostgresStore
from binnacle.application.config import BinnacleConfig
from binnacle.application.lifecycle import LifecycleEngine
from binnacle.domain.errors import AuthorityViolation, UnknownDomain
from binnacle.domain.models import (
    Actor,
    CompactDecision,
    Decision,
    DomainRecord,
    ExportBundle,
    HistoryRecord,
    NewDecision,
    QueueItemView,
    Tier,
    Transition,
)


class Binnacle:
    """The library's public API. Construction validates `config` and builds the
    store handle, but performs no I/O (`migrate()` is the explicit, host-invoked
    I/O step, docs/components/01 "Client API"). Every verb takes an explicit
    `Actor`; the client validates shape only (`Actor.__post_init__`) — authority
    (I-2) is the Lifecycle Engine's job, per verb, not this class's.
    """

    def __init__(self, config: BinnacleConfig) -> None:
        self._config = config
        self._store = PostgresStore(
            dsn=config.dsn,
            pool=config.pool,
            schema_name=config.schema_name,
            embedding_dim=config.embedding_dim,
        )
        self._engine = LifecycleEngine(self._store)

    async def migrate(self) -> None:
        """Apply pending schema migrations (delegates to the store). Host-invoked
        — never called implicitly by any other verb."""
        await self._store.migrate()

    async def aclose(self) -> None:
        """Close the pool this client's store opened itself; a no-op when
        `config.pool` was caller-supplied (the caller owns closing it)."""
        await self._store.aclose()

    # -- recording (FR-1, FR-4.4) --------------------------------------------

    async def record(self, nd: NewDecision, actor: Actor) -> Decision:
        """Record `nd` into the short-term tier (any actor). See
        `LifecycleEngine.record`."""
        return await self._engine.record(nd, actor)

    async def record_long_term(self, nd: NewDecision, actor: Actor) -> Decision:
        """Record `nd` directly into the long-term tier (FR-4.4, human only).
        See `LifecycleEngine.record_long_term`."""
        return await self._engine.record_long_term(nd, actor)

    # -- the review gate (FR-4) ------------------------------------------------

    async def recommend(
        self, decision_id: UUID, actor: Actor, reason: str | None = None
    ) -> int | None:
        """File a promotion recommendation (any actor). See
        `LifecycleEngine.recommend`."""
        return await self._engine.recommend(decision_id, actor, reason)

    async def promote(self, item_id: int, actor: Actor) -> Decision:
        """Execute a pending promotion verbatim (human only). See
        `LifecycleEngine.promote`."""
        return await self._engine.promote(item_id, actor)

    async def promote_refined(
        self, source_ids: Sequence[UUID], refined: NewDecision, actor: Actor
    ) -> Decision:
        """Consolidate one or more short-term sources into one human-authored
        long-term decision (FR-4.6, human only). See
        `LifecycleEngine.promote_refined`."""
        return await self._engine.promote_refined(source_ids, refined, actor)

    async def decline(self, item_id: int, actor: Actor, reason: str | None = None) -> None:
        """Decline a pending promotion (human only). See
        `LifecycleEngine.decline`."""
        await self._engine.decline(item_id, actor, reason)

    # -- direct status acts (FR-3) ---------------------------------------------

    async def discard(self, decision_id: UUID, actor: Actor, reason: str | None = None) -> None:
        """Discard a short-term decision (FR-3.3). See `LifecycleEngine.discard`."""
        await self._engine.discard(decision_id, actor, reason)

    async def supersede(self, new_id: UUID, old_id: UUID, actor: Actor) -> None:
        """Link `new_id` as superseding `old_id` (FR-5.2a). See
        `LifecycleEngine.supersede`."""
        await self._engine.supersede(new_id, old_id, actor)

    async def supplement(self, new_id: UUID, old_id: UUID, actor: Actor) -> None:
        """Link `new_id` as supplementing `old_id` (FR-5.3). See
        `LifecycleEngine.supplement`."""
        await self._engine.supplement(new_id, old_id, actor)

    async def reactivate(self, decision_id: UUID, actor: Actor) -> None:
        """Reactivate an archived decision (FR-3.4). See
        `LifecycleEngine.reactivate`."""
        await self._engine.reactivate(decision_id, actor)

    # -- queue resolution (FR-4.3) ----------------------------------------------

    async def apply_item(self, item_id: int, actor: Actor) -> None:
        """Execute a suggested link/supersede queue item (human only). See
        `LifecycleEngine.apply_item`."""
        await self._engine.apply_item(item_id, actor)

    async def dismiss_item(self, item_id: int, actor: Actor, reason: str | None = None) -> None:
        """Dismiss a queue item as noise (human only). See
        `LifecycleEngine.dismiss_item`."""
        await self._engine.dismiss_item(item_id, actor, reason)

    # -- queries (FR-6) -----------------------------------------------------

    async def relevant(
        self,
        domains: Sequence[str] | None = None,
        subject: tuple[str, str] | None = None,
        status: Sequence[str] = ("current",),
        tier: Tier | None = None,
        as_of: datetime | None = None,
        text: str | None = None,
        projection: Literal["compact", "full"] = "compact",
        limit: int = 50,
        include_archived: bool = False,
    ) -> "list[CompactDecision] | list[Decision]":
        """FR-6.1 relevance query. `projection='compact'` truncates `outcome` to
        `config.compact_outcome_chars` in SQL (FR-6.7); `'full'` returns the
        untruncated `Decision`."""
        compact_chars = self._config.compact_outcome_chars if projection == "compact" else None
        return await self._store.relevant(
            domains=domains,
            status=status,
            tier=tier,
            subject=subject,
            as_of=as_of,
            text=text,
            include_archived=include_archived,
            limit=limit,
            compact_chars=compact_chars,
        )

    async def history(self, decision_id: UUID) -> HistoryRecord:
        """FR-6.2: a decision's full record. See `StorePort.history`."""
        return await self._store.history(decision_id)

    async def queue(
        self,
        kinds: Sequence[str] | None = None,
        order: Literal["oldest", "shakiest", "domain"] = "oldest",
    ) -> list[QueueItemView]:
        """FR-4.3/6.4: open queue items. See `StorePort.open_queue`."""
        return await self._store.open_queue(kinds=kinds, order=order)

    async def changes(
        self,
        since: datetime | None = None,
        actions: Sequence[str] | None = None,
        actor: Actor | None = None,
    ) -> list[tuple[Transition, CompactDecision]]:
        """FR-6.5: the changes feed. See `StorePort.changes`."""
        return await self._store.changes(since, actions, actor)

    async def get_many(self, ids: Sequence[UUID]) -> list[Decision]:
        """FR-6.8: batch get-by-id. See `StorePort.get_many`."""
        return await self._store.get_many(ids)

    async def by_source(self, source: str, **filters: object) -> list[CompactDecision]:
        """FR-6.8: a source system's own decisions. See `StorePort.by_source`."""
        return await self._store.by_source(source, **filters)

    async def export(
        self,
        domains: Sequence[str] | None = None,
        tier: Tier | None = None,
        status: Sequence[str] | None = None,
    ) -> ExportBundle:
        """FR-6.6: filtered JSON-ready export. See `StorePort.export_rows`."""
        return await self._store.export_rows(domains=domains, tier=tier, status=status)

    # -- domain registry (FR-2) --------------------------------------------

    async def domains(self) -> list[DomainRecord]:
        """FR-2.1: every registered domain. See `StorePort.list_domains`."""
        return await self._store.list_domains()

    async def add_domain(self, name: str, description: str, actor: Actor) -> None:
        """Register a new domain, or re-register (and reactivate) an existing
        one under the same name (FR-2.1/2.2, human only)."""
        _require_human(actor, "add_domain")
        async with self._store.transaction() as tx:
            await self._store.upsert_domain(
                tx, name, description, True, actor.as_str(), "domain_created", None
            )

    async def update_domain(self, name: str, description: str, actor: Actor) -> None:
        """Rename a domain's description, preserving its current active flag
        (FR-2.2, human only).

        Raises:
            UnknownDomain: `name` is not a registered domain.
        """
        _require_human(actor, "update_domain")
        current = await self._get_domain(name)
        async with self._store.transaction() as tx:
            await self._store.upsert_domain(
                tx, name, description, current.active, actor.as_str(), "domain_updated", None
            )

    async def deactivate_domain(self, name: str, actor: Actor, reason: str | None = None) -> None:
        """Deactivate a domain, preserving its current description (FR-2.2,
        human only).

        Raises:
            UnknownDomain: `name` is not a registered domain.
        """
        _require_human(actor, "deactivate_domain")
        current = await self._get_domain(name)
        async with self._store.transaction() as tx:
            await self._store.upsert_domain(
                tx,
                name,
                current.description,
                False,
                actor.as_str(),
                "domain_deactivated",
                reason,
            )

    async def _get_domain(self, name: str) -> DomainRecord:
        for record in await self._store.list_domains():
            if record.name == name:
                return record
        raise UnknownDomain(name)


def _require_human(actor: Actor, verb: str) -> None:
    if actor.kind != "human":
        msg = f"{verb} requires a human actor"
        raise AuthorityViolation(msg)
