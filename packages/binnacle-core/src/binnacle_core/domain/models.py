"""Binnacle domain models."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# Type aliases
ActorKind = Literal["human", "agent", "engine"]
Tier = Literal["short_term", "long_term"]
ShortStatus = Literal["current", "promoted", "not_promoted", "superseded", "discarded", "archived"]
LongStatus = Literal["current", "superseded"]
LinkKind = Literal[
    "SUPERSEDES", "PARTIALLY_SUPERSEDES", "SUPPLEMENTS", "PROMOTED_FROM", "CONFLICTS_WITH"
]
QueueKind = Literal["promote", "link", "supersede", "conflict"]
RefRole = Literal["subject", "evidence"]
TransitionAction = Literal[
    "recorded",
    "recommended",
    "promoted",
    "declined",
    "discarded",
    "superseded",
    "partial_replacement_linked",
    "supplement_linked",
    "archived",
    "reactivated",
    "voided",
    "dismissed",
    "conflict_accepted",
]


@dataclass(frozen=True)
class Actor:
    """An actor who performs actions."""

    kind: ActorKind
    id: str

    def __post_init__(self) -> None:
        """Validate kind on construction."""
        if self.kind not in ("human", "agent", "engine"):
            msg = f"Invalid actor kind: {self.kind}"
            raise ValueError(msg)

    def as_str(self) -> str:
        """Return actor as 'kind:id' string."""
        return f"{self.kind}:{self.id}"

    @staticmethod
    def from_str(s: str) -> "Actor":
        """Parse actor from 'kind:id' string.

        Args:
            s: String in format 'kind:id' (e.g. 'agent:meridian/s1')

        Returns:
            Actor instance

        Raises:
            ValueError: If format is invalid
        """
        parts = s.split(":", 1)
        if len(parts) != 2:
            msg = f"Invalid actor string format: {s}"
            raise ValueError(msg)
        kind, actor_id = parts
        if kind not in ("human", "agent", "engine"):
            msg = f"Invalid actor kind: {kind}"
            raise ValueError(msg)
        return Actor(kind=kind, id=actor_id)  # type: ignore


class Ref(BaseModel):
    """A reference to external information."""

    role: RefRole
    kind: str
    identifier: str
    note: str | None = None


class OptionConsidered(BaseModel):
    """An option considered but not chosen."""

    option: str
    why_rejected: str


class NewDecision(BaseModel):
    """A new decision record to be stored."""

    domain: str
    scenario: str
    outcome: str
    reasoning: str
    source: str
    decision_id: UUID | None = None
    options_considered: list[OptionConsidered] = Field(default_factory=list)
    consequences: str | None = None
    confidence: float | None = None
    decided_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    refs: list[Ref] = Field(default_factory=list)
    supersedes: list[UUID] = Field(default_factory=list)
    supplements: list[UUID] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float | None) -> float | None:
        """Validate confidence is between 0 and 1."""
        if v is not None and (v < 0 or v > 1):
            msg = "confidence must be between 0 and 1"
            raise ValueError(msg)
        return v

    def content_hash(self) -> str:
        """Compute SHA256 hash of canonical JSON of content fields.

        Content fields include: domain, scenario, outcome, reasoning, options_considered,
        consequences, confidence, decided_at, valid_from, valid_until, refs, supersedes,
        supplements, source. Excluded: metadata, decision_id.

        Returns:
            Hex-encoded SHA256 hash
        """
        # Build canonical content dict with only content fields
        content = {
            "domain": self.domain,
            "scenario": self.scenario,
            "outcome": self.outcome,
            "reasoning": self.reasoning,
            "source": self.source,
            "options_considered": [
                {"option": opt.option, "why_rejected": opt.why_rejected}
                for opt in self.options_considered
            ],
            "consequences": self.consequences,
            "confidence": self.confidence,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "refs": sorted(
                [
                    {
                        "role": ref.role,
                        "kind": ref.kind,
                        "identifier": ref.identifier,
                        "note": ref.note,
                    }
                    for ref in self.refs
                ],
                key=lambda r: (r["role"], r["kind"], r["identifier"]),
            ),
            "supersedes": sorted(str(uuid) for uuid in self.supersedes),
            "supplements": sorted(str(uuid) for uuid in self.supplements),
        }

        # Canonical JSON: sorted keys, compact output
        canonical_json = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode()).hexdigest()


@dataclass(frozen=True)
class Decision:
    """A stored decision record with metadata."""

    decision_id: UUID
    domain: str
    tier: Tier
    status: ShortStatus | LongStatus
    scenario: str
    outcome: str
    reasoning: str
    source: str
    recorded_by: Actor
    recorded_at: datetime
    decided_at: datetime | None = None
    options_considered: list[OptionConsidered] = field(default_factory=list)
    consequences: str | None = None
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    refs: list[Ref] = field(default_factory=list)
    supersedes: list[UUID] = field(default_factory=list)
    supplements: list[UUID] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1


@dataclass(frozen=True)
class CompactDecision:
    """A compact representation of a decision for listing.

    `partially_replaced` is true when at least one later decision replaced part
    of this one (FR-3.1c). The flag travels with the compact projection, and the
    `part` text deliberately does not: naming what was replaced is the full
    read's job (`history().partial_replacers`), while a listing only has to stop
    short of implying nothing was replaced.

    It is carried here even though supplements are NOT (FR-5.3), because the two
    relations are asymmetric: a supplement **adds**, so a consumer that misses
    one merely lacks context, while a partial replacement **subtracts**, so a
    consumer that misses one relies on a ruling that is gone — with a `status`
    of `current` that is true and still misleading. That is the exact failure
    FR-3.1c exists to prevent.

    No default: a store that omitted the flag would report "nothing here was
    replaced", which is the misleading answer rather than an absent one.
    """

    id: UUID
    domain: str
    tier: Tier
    status: ShortStatus | LongStatus
    outcome_truncated: str
    subject_refs: list[Ref]
    partially_replaced: bool


@dataclass(frozen=True)
class PrecedentHit:
    """One precedent() result (docs/binnacle-core/components/04-query-and-assist.md
    "precedent()"): a compact projection paired with its k-NN cosine
    similarity to the query. `decision.status` carries superseded/`not_promoted`
    history when `include_dead=True` (FR-6.3) — labeled via that field, not
    hidden; similarity is surfaced as-is, never silently thresholded.
    """

    decision: CompactDecision
    similarity: float


@dataclass(frozen=True)
class Transition:
    """A state transition in a decision's lifecycle.

    `reason`, `new_status`, and `payload` are all `None`-able: the schema columns
    are nullable (ARCHITECTURE.md §4) and `apply_transition` legitimately persists
    NULL for each — most commonly `new_status=None` on a `recommended` transition,
    which never changes status.
    """

    transition_id: int
    decision_id: UUID
    action: TransitionAction
    actor: Actor
    at: datetime
    reason: str | None
    new_status: ShortStatus | LongStatus | None
    payload: dict[str, Any] | None


@dataclass(frozen=True)
class Link:
    """A link between two decisions.

    `part` is the replaced-part description a `PARTIALLY_SUPERSEDES` link
    carries (FR-3.1a): prose naming which ruling of `to_id` the decision
    `from_id` replaced, written for a human and parsed by nothing (ADR 0002).
    It is `None` on every other kind, and never `None` on this one — the
    `links` CHECK constraint (migration 0005) enforces both directions, so a
    partial-replacement link with no part cannot exist in the database.
    """

    from_id: UUID
    to_id: UUID
    kind: LinkKind
    part: str | None = None


@dataclass(frozen=True)
class PartialReplacement:
    """One decision that partially replaced another, paired with the `part` it
    replaced (FR-3.1a/3.1c) — the shape `HistoryRecord.partial_replacers` uses
    so a caller reading "something here was replaced" can read *what* in the
    same breath, instead of joining `links` back by id.

    Per ADR 0002, the presence of one of these means a ruling of the subject
    decision is gone and Binnacle **cannot tell whether it was the one a
    citation relied upon**; a consumer surfacing it must say so rather than
    implying the whole decision has fallen (FR-3.1c).
    """

    decision: Decision
    part: str


@dataclass(frozen=True)
class QueueItem:
    """An item in a promotion/linking queue.

    `rationale` and `confidence` are `None`-able: the schema columns are nullable
    (ARCHITECTURE.md §4) and `enqueue` accepts `None` for both — the `shakiest`
    queue ordering (docs/binnacle-core/components/04-query-and-assist.md) depends on this: it
    falls back from item confidence to the decision's own confidence to 1.0 last,
    which requires `None` to be representable at every step.
    """

    item_id: int
    kind: QueueKind
    decision_id: UUID
    target_id: UUID | None
    proposed_by: Actor
    proposed_at: datetime
    rationale: str | None
    confidence: float | None
    resolved: bool


@dataclass(frozen=True)
class CandidatePair:
    """A pair of similar decisions for linking suggestions."""

    decision: CompactDecision
    other: CompactDecision
    similarity: float


@dataclass(frozen=True)
class Suggestion:
    """A linking suggestion between decisions."""

    kind: Literal["supersedes", "supplements", "conflicts", "unrelated"]
    rationale: str
    confidence: float


@dataclass(frozen=True)
class PromotionAssessment:
    """An assessment of a decision's readiness for promotion."""

    decision_id: UUID
    recommend: bool
    rationale: str
    confidence: float


@dataclass(frozen=True)
class DomainRecord:
    """One row of the governed domain registry (FR-2)."""

    name: str
    description: str
    active: bool


@dataclass(frozen=True)
class HistoryRecord:
    """A decision's full record (FR-6.2): content and refs (on `decision`),
    transitions in order, every link touching the decision, both supersession
    chains (recursive over `links` kind SUPERSEDES), supplements — decisions
    that supplement this one (FR-5.3) — `partial_replacers` — decisions that
    replaced only *part* of this one, each with the part it replaced
    (FR-3.1a/3.1c) — and conflicts — decisions this one has an acknowledged
    `CONFLICTS_WITH` link with, from `resolve_conflict`'s accept path (FR-7.2).
    Includes archived/discarded entries throughout; history hides nothing.

    `predecessors`/`successors` are whole replacement ONLY: a partial replacer
    is never a successor, because the subject is still the authority for every
    ruling it did not touch (FR-3.1a). The two relations are reported in
    separate fields precisely so a caller can ask "is anything here still in
    force?" and get a true answer (FR-3.1c).
    """

    decision: Decision
    transitions: list[Transition]
    links: list[Link]
    predecessors: list[Decision]
    successors: list[Decision]
    supplements: list[Decision]
    partial_replacers: list[PartialReplacement]
    conflicts: list[Decision]


@dataclass(frozen=True)
class QueueItemView:
    """One open queue item plus the fields its orderings need
    (docs/binnacle-core/components/04-query-and-assist.md `queue()`): the item itself (carrying
    `proposed_by` as recommender, `rationale`, and its own `confidence`), the
    subject decision's `domain` (the `domain` ordering), and the subject
    decision's own `confidence` (the `shakiest` ordering's fallback when the item
    carries none, itself falling back to 1.0, sorted last).
    """

    item: QueueItem
    domain: str
    decision_confidence: float | None
    age: timedelta


@dataclass(frozen=True)
class BackfillSummary:
    """`backfill_embeddings()` sweep result (docs/binnacle-core/components/04-query-and-assist.md
    "The sweeps"): how many decisions from the unembedded backlog were embedded
    and upserted this call. Zero when the backlog is empty -- the sweep no-ops
    cleanly rather than erroring.
    """

    embedded: int


@dataclass(frozen=True)
class DiscoverySummary:
    """`discover()` sweep result (docs/binnacle-core/components/04-query-and-assist.md "The
    sweeps"; FR-7.4): counts from both halves of the sweep -- relationship
    discovery over newly embedded decisions (`decisions_processed`,
    `suggestions_enqueued`, `suggestions_deduped`, `suggestions_below_floor`),
    and promotion assessment over aging unrecommended decisions
    (`promotions_recommended`). All zero when no `Suggester` is configured
    (the sweep no-ops cleanly) or when both cursors are empty.
    """

    decisions_processed: int
    suggestions_enqueued: int
    suggestions_deduped: int
    suggestions_below_floor: int
    promotions_recommended: int


@dataclass(frozen=True)
class ArchivalSummary:
    """`archive_stale()` sweep result (FR-3.4): how many short-term decisions
    crossed the auto-archival clock and were archived this call. Zero when
    nothing is clock-eligible.
    """

    archived: int


@dataclass(frozen=True)
class ExportBundle:
    """A filtered export (FR-6.6): decisions (each carrying its own refs), every
    link and transition touching them, and the full domains registry. Embeddings
    are deliberately excluded (derived, rebuildable).
    """

    schema_version: int
    decisions: list[Decision]
    links: list[Link]
    transitions: list[Transition]
    domains: list[DomainRecord]


@dataclass(frozen=True)
class Page[T]:
    """One page of a keyset-paginated read. `next_cursor` is opaque: pass it
    back as `after=` to fetch the following page, or `None` when this page is
    the last. Carries no total count -- see `Binnacle.relevant_count()`."""

    items: list[T]
    next_cursor: str | None


@dataclass(frozen=True)
class DomainSummary:
    """One registry row with its decision count (FR-6.10). Domains with no
    decisions are included, with `decision_count == 0`."""

    name: str
    description: str
    active: bool
    decision_count: int
