"""The archival sweep (docs/components/04-query-and-assist.md "The sweeps";
REQUIREMENTS FR-3.4). A free function over `StorePort` + `LifecycleEngine`,
same shape as `query.precedent`/`discovery.backfill_embeddings` -- no state
held between calls.
"""

from datetime import UTC, datetime, timedelta

from binnacle.application.lifecycle import LifecycleEngine
from binnacle.application.ports import StorePort
from binnacle.domain.models import Actor, ArchivalSummary

_ENGINE_ACTOR = Actor("engine", "binnacle")


async def archive_stale(
    store: StorePort, engine: LifecycleEngine, archival_age_days: int
) -> ArchivalSummary:
    """FR-3.4: auto-archive every short-term decision whose clock has expired.

    `store.archival_eligible(cutoff)` already restricts to short-term
    `current`/`not_promoted` decisions recorded before the cutoff with no
    open queue item referencing them (FR-3.4: "A decision with OPEN queue
    items is never archival-eligible -- pending human attention stops the
    clock"), so every returned id is archived unconditionally through
    `LifecycleEngine.archive` in one atomic call, attributed to the engine
    actor (FR-7.3: clock-driven mechanism, not judgment -- no `Suggester`
    involved).

    Args:
        archival_age_days: `BinnacleConfig.archival_age_days` -- decisions
            recorded before `now - archival_age_days` are eligible.

    Returns:
        `ArchivalSummary` counting decisions archived; zero when nothing is
        clock-eligible (idempotent no-op).
    """
    cutoff = datetime.now(UTC) - timedelta(days=archival_age_days)
    ids = await store.archival_eligible(cutoff)
    if not ids:
        return ArchivalSummary(archived=0)
    count = await engine.archive(ids, _ENGINE_ACTOR)
    return ArchivalSummary(archived=count)
