"""Read-only and write decision endpoints: direct translations of `Binnacle`'s
query and mutating methods (GUIDELINES §8: no business logic in
transport-layer code). Reads are unattributed -- none of them take an actor.
Writes are the first endpoints that carry an attested actor, resolved by the
host-supplied `get_actor` and never taken from the request body or headers
(see `router.ActorResolver`'s docstring)."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from binnacle_core import (
    Actor,
    Binnacle,
    CompactDecision,
    Decision,
    HistoryRecord,
    NewDecision,
    Page,
    Tier,
)
from binnacle_router.errors import BinnacleAPIRoute
from binnacle_router.params import paired

SortKey = Literal["decided_at", "recorded_at", "last_touched_at", "valid_until"]
Order = Literal["asc", "desc"]


class _FilterFields(BaseModel):
    """Filters shared by `GET /decisions` and `GET /decisions/count`. A
    FastAPI query-parameter model cannot share a route with other, bare
    query parameters (verified by spike: doing so makes FastAPI treat the
    whole model as one missing scalar param named after it) -- so each
    endpoint below declares exactly one such model, and `DecisionsQuery`
    layers its pagination/presentation fields on top of this one rather than
    duplicating the filter fields."""

    domains: list[str] | None = None
    subject_kind: str | None = None
    subject_identifier: str | None = None
    evidence_kind: str | None = None
    evidence_identifier: str | None = None
    status: list[str] = ["current"]
    tier: Tier | None = None
    as_of: datetime | None = None
    expiring_before: datetime | None = None
    text: str | None = None
    include_archived: bool = False


class DecisionsQuery(_FilterFields):
    """`GET /decisions`'s full query parameter set: filters plus pagination
    and presentation."""

    sort: SortKey = "recorded_at"
    order: Order = "desc"
    limit: int = 50
    after: str | None = None
    projection: Literal["compact", "full"] = "compact"


class CountQuery(_FilterFields):
    """`GET /decisions/count`'s query parameters -- filters only.
    `extra="forbid"` actively rejects pagination/presentation parameters
    (`sort`/`order`/`after`/`limit`/`projection`) rather than silently
    ignoring them -- a count these cannot affect, and FastAPI ignores
    unknown query parameters by default."""

    model_config = ConfigDict(extra="forbid")


class BatchGetRequest(BaseModel):
    ids: list[UUID]


def decision_read_router(binnacle: Binnacle) -> APIRouter:
    router = APIRouter(route_class=BinnacleAPIRoute)

    @router.get("/decisions")
    async def list_decisions(
        filters: Annotated[DecisionsQuery, Query()],
    ) -> Page[CompactDecision] | Page[Decision]:
        subject = paired(
            filters.subject_kind,
            filters.subject_identifier,
            kind_param="subject_kind",
            identifier_param="subject_identifier",
        )
        evidence = paired(
            filters.evidence_kind,
            filters.evidence_identifier,
            kind_param="evidence_kind",
            identifier_param="evidence_identifier",
        )
        # Branching on a literal per call (rather than passing `filters.projection`
        # straight through) mirrors `Binnacle.relevant()`'s own docstring: with this
        # many `Optional` parameters, a call site carrying a union-typed `projection`
        # exceeds mypy's overload-combination limit ("Not all union combinations
        # were tried"). A literal at each call site resolves to exactly one overload.
        if filters.projection == "compact":
            return await binnacle.relevant(
                domains=filters.domains,
                subject=subject,
                evidence=evidence,
                status=filters.status,
                tier=filters.tier,
                as_of=filters.as_of,
                expiring_before=filters.expiring_before,
                text=filters.text,
                projection="compact",
                sort=filters.sort,
                order=filters.order,
                limit=filters.limit,
                after=filters.after,
                include_archived=filters.include_archived,
            )
        return await binnacle.relevant(
            domains=filters.domains,
            subject=subject,
            evidence=evidence,
            status=filters.status,
            tier=filters.tier,
            as_of=filters.as_of,
            expiring_before=filters.expiring_before,
            text=filters.text,
            projection="full",
            sort=filters.sort,
            order=filters.order,
            limit=filters.limit,
            after=filters.after,
            include_archived=filters.include_archived,
        )

    @router.get("/decisions/count")
    async def count_decisions(filters: Annotated[CountQuery, Query()]) -> dict[str, int]:
        subject = paired(
            filters.subject_kind,
            filters.subject_identifier,
            kind_param="subject_kind",
            identifier_param="subject_identifier",
        )
        evidence = paired(
            filters.evidence_kind,
            filters.evidence_identifier,
            kind_param="evidence_kind",
            identifier_param="evidence_identifier",
        )
        count = await binnacle.relevant_count(
            domains=filters.domains,
            subject=subject,
            evidence=evidence,
            status=filters.status,
            tier=filters.tier,
            as_of=filters.as_of,
            expiring_before=filters.expiring_before,
            text=filters.text,
            include_archived=filters.include_archived,
        )
        return {"count": count}

    @router.get("/decisions/by_source")
    async def decisions_by_source(
        source: str,
        status: Annotated[list[str] | None, Query()] = None,
        tier: Tier | None = None,
        limit: int | None = None,
    ) -> list[CompactDecision]:
        filter_kwargs: dict[str, object] = {}
        if status is not None:
            filter_kwargs["status"] = status
        if tier is not None:
            filter_kwargs["tier"] = tier
        if limit is not None:
            filter_kwargs["limit"] = limit
        return await binnacle.by_source(source, **filter_kwargs)

    @router.post("/decisions:batch_get")
    async def batch_get_decisions(body: BatchGetRequest) -> list[Decision]:
        return await binnacle.get_many(body.ids)

    @router.get("/decisions/{decision_id}/history")
    async def decision_history(decision_id: UUID) -> HistoryRecord:
        return await binnacle.history(decision_id)

    return router


class PromoteRefinedRequest(BaseModel):
    source_ids: list[UUID]
    refined: NewDecision


class RelationshipRequest(BaseModel):
    """`kind` is closed to the two relationships a caller may curate directly.
    `PROMOTED_FROM` is internal provenance and `CONFLICTS_WITH` is set only by
    `resolve_conflict` -- neither is settable here, so FastAPI rejects any
    other value with a 422 rather than this endpoint silently widening what
    it accepts."""

    kind: Literal["SUPERSEDES", "SUPPLEMENTS"]
    target_id: UUID


class ReasonRequest(BaseModel):
    reason: str | None = None


def decision_write_router(
    binnacle: Binnacle, get_actor: Callable[..., Awaitable[Actor]]
) -> APIRouter:
    router = APIRouter(route_class=BinnacleAPIRoute)

    @router.post("/decisions")
    async def record_decision(
        nd: NewDecision, actor: Annotated[Actor, Depends(get_actor)]
    ) -> Decision:
        return await binnacle.record(nd, actor=actor)

    @router.post("/decisions/long_term")
    async def record_long_term_decision(
        nd: NewDecision, actor: Annotated[Actor, Depends(get_actor)]
    ) -> Decision:
        return await binnacle.record_long_term(nd, actor=actor)

    @router.post("/decisions:promote_refined")
    async def promote_refined_decision(
        body: PromoteRefinedRequest, actor: Annotated[Actor, Depends(get_actor)]
    ) -> Decision:
        return await binnacle.promote_refined(body.source_ids, body.refined, actor=actor)

    @router.post("/decisions/{decision_id}/relationships")
    async def create_relationship(
        decision_id: UUID,
        body: RelationshipRequest,
        actor: Annotated[Actor, Depends(get_actor)],
    ) -> None:
        if body.kind == "SUPERSEDES":
            await binnacle.supersede(decision_id, body.target_id, actor=actor)
        else:
            await binnacle.supplement(decision_id, body.target_id, actor=actor)

    @router.post("/decisions/{decision_id}:recommend")
    async def recommend_decision(
        decision_id: UUID,
        body: ReasonRequest,
        actor: Annotated[Actor, Depends(get_actor)],
    ) -> dict[str, int | None]:
        item_id = await binnacle.recommend(decision_id, actor=actor, reason=body.reason)
        return {"item_id": item_id}

    @router.post("/decisions/{decision_id}:discard")
    async def discard_decision(
        decision_id: UUID,
        body: ReasonRequest,
        actor: Annotated[Actor, Depends(get_actor)],
    ) -> None:
        await binnacle.discard(decision_id, actor=actor, reason=body.reason)

    @router.post("/decisions/{decision_id}:reactivate")
    async def reactivate_decision(
        decision_id: UUID, actor: Annotated[Actor, Depends(get_actor)]
    ) -> None:
        await binnacle.reactivate(decision_id, actor=actor)

    return router
