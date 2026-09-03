"""Binnacle domain models."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# Type aliases
ActorKind = Literal["human", "agent", "engine"]
Tier = Literal["short_term", "long_term"]
ShortStatus = Literal["current", "promoted", "not_promoted", "superseded", "discarded", "archived"]
LongStatus = Literal["current", "superseded"]
LinkKind = Literal["SUPERSEDES", "SUPPLEMENTS", "PROMOTED_FROM"]
QueueKind = Literal["promote", "link", "supersede"]
RefRole = Literal["subject", "evidence"]
TransitionAction = Literal[
    "recorded",
    "recommended",
    "promoted",
    "declined",
    "discarded",
    "superseded",
    "supplement_linked",
    "archived",
    "reactivated",
    "voided",
    "dismissed",
]


class Actor(BaseModel):
    """An actor who performs actions."""

    model_config = {"frozen": True}

    kind: ActorKind
    id: str

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
    options_considered: list[OptionConsidered] = None  # type: ignore
    consequences: str | None = None
    confidence: float | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    refs: list[Ref] = None  # type: ignore
    supersedes: list[UUID] = None  # type: ignore
    supplements: list[UUID] = None  # type: ignore
    metadata: dict[str, Any] = None  # type: ignore
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Initialize default empty collections."""
        if self.options_considered is None:
            object.__setattr__(self, "options_considered", [])
        if self.refs is None:
            object.__setattr__(self, "refs", [])
        if self.supersedes is None:
            object.__setattr__(self, "supersedes", [])
        if self.supplements is None:
            object.__setattr__(self, "supplements", [])
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})


@dataclass(frozen=True)
class CompactDecision:
    """A compact representation of a decision for listing."""

    id: UUID
    domain: str
    tier: Tier
    status: ShortStatus | LongStatus
    outcome_truncated: str
    subject_refs: list[Ref]


@dataclass(frozen=True)
class Transition:
    """A state transition in a decision's lifecycle."""

    transition_id: UUID
    decision_id: UUID
    action: TransitionAction
    actor: Actor
    at: datetime
    reason: str
    new_status: ShortStatus | LongStatus
    payload: dict[str, Any]


@dataclass(frozen=True)
class Link:
    """A link between two decisions."""

    from_id: UUID
    to_id: UUID
    kind: LinkKind


@dataclass(frozen=True)
class QueueItem:
    """An item in a promotion/linking queue."""

    item_id: UUID
    kind: QueueKind
    decision_id: UUID
    target_id: UUID
    proposed_by: Actor
    proposed_at: datetime
    rationale: str
    confidence: float
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

    kind: Literal["supersedes", "supplements", "unrelated"]
    rationale: str
    confidence: float


@dataclass(frozen=True)
class PromotionAssessment:
    """An assessment of a decision's readiness for promotion."""

    decision_id: UUID
    recommend: bool
    rationale: str
    confidence: float
