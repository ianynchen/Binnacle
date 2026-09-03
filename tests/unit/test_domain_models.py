"""Unit tests for domain models and errors."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from binnacle.domain.errors import (
    AuthorityViolation,
    BinnacleError,
    ConfigError,
    DecisionNotFound,
    EmbeddingDimensionMismatch,
    IdempotencyConflict,
    InvalidTransition,
    ItemAlreadyResolved,
    ItemNotFound,
    UnknownDomain,
)
from binnacle.domain.models import (
    Actor,
    CandidatePair,
    CompactDecision,
    Link,
    NewDecision,
    OptionConsidered,
    PromotionAssessment,
    QueueItem,
    Ref,
    Suggestion,
    Transition,
)


class TestErrorHierarchy:
    """Test error classes and hierarchy."""

    def test_binnacle_error_exists(self) -> None:
        """BinnacleError is the root exception."""
        exc = BinnacleError("test")
        assert isinstance(exc, Exception)

    def test_config_error_is_binnacle_error(self) -> None:
        """ConfigError inherits from BinnacleError."""
        exc = ConfigError("test")
        assert isinstance(exc, BinnacleError)

    def test_unknown_domain_is_binnacle_error(self) -> None:
        """UnknownDomain inherits from BinnacleError."""
        exc = UnknownDomain("test")
        assert isinstance(exc, BinnacleError)

    def test_decision_not_found_is_binnacle_error(self) -> None:
        """DecisionNotFound inherits from BinnacleError."""
        exc = DecisionNotFound("test")
        assert isinstance(exc, BinnacleError)

    def test_item_not_found_is_binnacle_error(self) -> None:
        """ItemNotFound inherits from BinnacleError."""
        exc = ItemNotFound("test")
        assert isinstance(exc, BinnacleError)

    def test_embedding_dimension_mismatch_is_binnacle_error(self) -> None:
        """EmbeddingDimensionMismatch inherits from BinnacleError."""
        exc = EmbeddingDimensionMismatch("test")
        assert isinstance(exc, BinnacleError)

    def test_item_already_resolved_is_binnacle_error(self) -> None:
        """ItemAlreadyResolved inherits from BinnacleError."""
        exc = ItemAlreadyResolved("test")
        assert isinstance(exc, BinnacleError)

    def test_invalid_transition_carries_context(self) -> None:
        """InvalidTransition carries from_status and attempted_action."""
        exc = InvalidTransition(
            from_status="current", attempted_action="promoted", message="Not allowed"
        )
        assert exc.from_status == "current"
        assert exc.attempted_action == "promoted"
        assert "current" in str(exc)
        assert "promoted" in str(exc)
        assert isinstance(exc, BinnacleError)

    def test_authority_violation_is_binnacle_error(self) -> None:
        """AuthorityViolation inherits from BinnacleError."""
        exc = AuthorityViolation("User lacks permissions")
        assert isinstance(exc, BinnacleError)
        assert "permissions" in str(exc).lower() or "authority" in str(exc).lower()

    def test_idempotency_conflict_is_binnacle_error(self) -> None:
        """IdempotencyConflict inherits from BinnacleError."""
        exc = IdempotencyConflict("Duplicate request")
        assert isinstance(exc, BinnacleError)
        assert "duplicate" in str(exc).lower() or "idempotency" in str(exc).lower()


class TestActor:
    """Test Actor model."""

    def test_actor_creation(self) -> None:
        """Actor can be created with kind and id."""
        actor = Actor(kind="human", id="alice")
        assert actor.kind == "human"
        assert actor.id == "alice"

    def test_actor_kind_validation(self) -> None:
        """Actor kind must be human, agent, or engine."""
        with pytest.raises(ValueError):
            Actor(kind="invalid", id="alice")  # type: ignore

    def test_actor_as_str(self) -> None:
        """Actor.as_str() returns 'kind:id' format."""
        actor = Actor(kind="agent", id="meridian/s1")
        assert actor.as_str() == "agent:meridian/s1"

    def test_actor_from_str_simple(self) -> None:
        """Actor.from_str() parses 'kind:id' format."""
        actor = Actor.from_str("agent:meridian/s1")
        assert actor.kind == "agent"
        assert actor.id == "meridian/s1"

    def test_actor_from_str_human(self) -> None:
        """Actor.from_str() parses human kind."""
        actor = Actor.from_str("human:alice")
        assert actor.kind == "human"
        assert actor.id == "alice"

    def test_actor_from_str_with_slashes(self) -> None:
        """Actor.from_str() handles ids with slashes."""
        actor = Actor.from_str("engine:s1/stage/0")
        assert actor.kind == "engine"
        assert actor.id == "s1/stage/0"

    def test_actor_round_trip_with_colon_in_id(self) -> None:
        """Actor round-trip preserves colon in id."""
        # Create actor with colon in id
        actor = Actor(kind="agent", id="meridian/sess:1")
        # Serialize to string
        serialized = actor.as_str()
        assert serialized == "agent:meridian/sess:1"
        # Deserialize from string
        deserialized = Actor.from_str(serialized)
        assert deserialized.kind == actor.kind
        assert deserialized.id == actor.id

    def test_actor_frozen(self) -> None:
        """Actor is frozen (immutable)."""
        actor = Actor(kind="human", id="alice")
        with pytest.raises((AttributeError, ValueError)):  # FrozenInstanceError or AttributeError
            actor.kind = "agent"  # type: ignore


class TestRef:
    """Test Ref model."""

    def test_ref_creation(self) -> None:
        """Ref can be created with required fields."""
        ref = Ref(role="subject", kind="ticket", identifier="ABC-123")
        assert ref.role == "subject"
        assert ref.kind == "ticket"
        assert ref.identifier == "ABC-123"
        assert ref.note is None

    def test_ref_with_note(self) -> None:
        """Ref can include an optional note."""
        ref = Ref(
            role="evidence",
            kind="meeting",
            identifier="2026-01-15",
            note="Discussed at standup",
        )
        assert ref.note == "Discussed at standup"

    def test_ref_role_validation(self) -> None:
        """Ref role must be subject or evidence."""
        with pytest.raises(ValidationError):
            Ref(role="invalid", kind="ticket", identifier="ABC-123")  # type: ignore


class TestOptionConsidered:
    """Test OptionConsidered model."""

    def test_option_considered_creation(self) -> None:
        """OptionConsidered has option and why_rejected fields."""
        opt = OptionConsidered(option="Use Redis", why_rejected="Overkill for our scale")
        assert opt.option == "Use Redis"
        assert opt.why_rejected == "Overkill for our scale"

    def test_option_considered_required_fields(self) -> None:
        """OptionConsidered requires both fields."""
        with pytest.raises(ValidationError):
            OptionConsidered(option="Use Redis")  # type: ignore


class TestNewDecision:
    """Test NewDecision model."""

    def test_new_decision_minimal(self) -> None:
        """NewDecision can be created with required fields."""
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
        )
        assert decision.domain == "database"
        assert decision.scenario == "Need to store structured user data"
        assert decision.outcome == "Chose PostgreSQL"
        assert decision.reasoning == "Reliable, widely used, good performance"
        assert decision.source == "team-standup"
        assert decision.decision_id is None
        assert decision.options_considered == []
        assert decision.consequences is None
        assert decision.confidence is None
        assert decision.decided_at is None
        assert decision.valid_from is None
        assert decision.valid_until is None
        assert decision.refs == []
        assert decision.supersedes == []
        assert decision.supplements == []
        assert decision.metadata == {}

    def test_new_decision_with_decision_id(self) -> None:
        """NewDecision can be created with a decision_id."""
        decision_id = uuid4()
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            decision_id=decision_id,
        )
        assert decision.decision_id == decision_id

    def test_new_decision_confidence_valid_bounds(self) -> None:
        """Confidence must be between 0 and 1."""
        # Valid: 0.0
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            confidence=0.0,
        )
        assert decision.confidence == 0.0

        # Valid: 0.5
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            confidence=0.5,
        )
        assert decision.confidence == 0.5

        # Valid: 1.0
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            confidence=1.0,
        )
        assert decision.confidence == 1.0

    def test_new_decision_confidence_invalid_too_high(self) -> None:
        """Confidence > 1.0 is invalid."""
        with pytest.raises(ValidationError):
            NewDecision(
                domain="database",
                scenario="Need to store structured user data",
                outcome="Chose PostgreSQL",
                reasoning="Reliable, widely used, good performance",
                source="team-standup",
                confidence=1.5,  # type: ignore
            )

    def test_new_decision_confidence_invalid_negative(self) -> None:
        """Confidence < 0.0 is invalid."""
        with pytest.raises(ValidationError):
            NewDecision(
                domain="database",
                scenario="Need to store structured user data",
                outcome="Chose PostgreSQL",
                reasoning="Reliable, widely used, good performance",
                source="team-standup",
                confidence=-0.1,  # type: ignore
            )

    def test_new_decision_with_options_considered(self) -> None:
        """NewDecision can include considered options."""
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            options_considered=[
                OptionConsidered(option="MySQL", why_rejected="Less features"),
                OptionConsidered(option="MongoDB", why_rejected="Overkill for schema"),
            ],
        )
        assert len(decision.options_considered) == 2

    def test_new_decision_with_refs(self) -> None:
        """NewDecision can include references."""
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            refs=[
                Ref(role="subject", kind="ticket", identifier="DB-42"),
                Ref(role="evidence", kind="blog", identifier="postgresql-perf-2025"),
            ],
        )
        assert len(decision.refs) == 2

    def test_new_decision_with_supersedes_and_supplements(self) -> None:
        """NewDecision can reference other decisions."""
        old_id = uuid4()
        related_id = uuid4()
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            supersedes=[old_id],
            supplements=[related_id],
        )
        assert old_id in decision.supersedes
        assert related_id in decision.supplements

    def test_new_decision_with_metadata(self) -> None:
        """NewDecision can include arbitrary metadata."""
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            metadata={"team": "backend", "priority": "high"},
        )
        assert decision.metadata == {"team": "backend", "priority": "high"}

    def test_new_decision_full(self) -> None:
        """NewDecision can be created with all fields."""
        now = datetime.now(UTC)
        later = datetime.now(UTC)
        decision_id = uuid4()
        old_id = uuid4()
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            decision_id=decision_id,
            options_considered=[
                OptionConsidered(option="MySQL", why_rejected="Less features"),
            ],
            consequences="Must manage schema migrations",
            confidence=0.95,
            decided_at=now,
            valid_from=now,
            valid_until=later,
            refs=[Ref(role="subject", kind="ticket", identifier="DB-42")],
            supersedes=[old_id],
            supplements=[],
            metadata={"team": "backend"},
        )
        assert decision.decision_id == decision_id
        assert decision.consequences == "Must manage schema migrations"
        assert decision.confidence == 0.95
        assert decision.decided_at == now
        assert decision.valid_from == now
        assert decision.valid_until == later


class TestContentHash:
    """Test NewDecision.content_hash() method."""

    def test_content_hash_basic(self) -> None:
        """content_hash() returns a string."""
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
        )
        hash_value = decision.content_hash()
        assert isinstance(hash_value, str)
        assert len(hash_value) == 64  # SHA256 hex digest

    def test_content_hash_stable(self) -> None:
        """content_hash() is stable across multiple calls."""
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
        )
        hash1 = decision.content_hash()
        hash2 = decision.content_hash()
        assert hash1 == hash2

    def test_content_hash_changes_on_outcome_change(self) -> None:
        """content_hash() differs when outcome changes."""
        decision1 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
        )
        decision2 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose MySQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
        )
        assert decision1.content_hash() != decision2.content_hash()

    def test_content_hash_stable_with_metadata_change(self) -> None:
        """content_hash() is stable even when metadata changes."""
        decision1 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            metadata={"team": "backend"},
        )
        decision2 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            metadata={"team": "backend", "priority": "high"},
        )
        assert decision1.content_hash() == decision2.content_hash()

    def test_content_hash_stable_with_decision_id_change(self) -> None:
        """content_hash() is stable even when decision_id changes."""
        id1 = uuid4()
        id2 = uuid4()
        decision1 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            decision_id=id1,
        )
        decision2 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            decision_id=id2,
        )
        assert decision1.content_hash() == decision2.content_hash()

    def test_content_hash_stable_with_refs_reordered(self) -> None:
        """content_hash() is stable even when refs order differs."""
        ref1 = Ref(role="subject", kind="ticket", identifier="ABC-123")
        ref2 = Ref(role="evidence", kind="blog", identifier="postgresql-perf-2025")

        decision1 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            refs=[ref1, ref2],
        )
        decision2 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            refs=[ref2, ref1],
        )
        assert decision1.content_hash() == decision2.content_hash()

    def test_content_hash_changes_on_ref_content_change(self) -> None:
        """content_hash() differs when ref content changes."""
        decision1 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            refs=[Ref(role="subject", kind="ticket", identifier="ABC-123")],
        )
        decision2 = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
            refs=[Ref(role="subject", kind="ticket", identifier="ABC-124")],
        )
        assert decision1.content_hash() != decision2.content_hash()

    def test_content_hash_canonical_json(self) -> None:
        """content_hash() uses canonical JSON (sorted keys)."""
        decision = NewDecision(
            domain="database",
            scenario="Need to store structured user data",
            outcome="Chose PostgreSQL",
            reasoning="Reliable, widely used, good performance",
            source="team-standup",
        )
        hash_value = decision.content_hash()
        # Hash should be consistent and deterministic
        assert hash_value == decision.content_hash()


class TestCompactDecision:
    """Test CompactDecision dataclass."""

    def test_compact_decision_creation(self) -> None:
        """CompactDecision has required fields."""
        decision_id = uuid4()
        subject_ref = Ref(role="subject", kind="ticket", identifier="ABC-123")
        decision = CompactDecision(
            id=decision_id,
            domain="database",
            tier="short_term",
            status="current",
            outcome_truncated="Chose PostgreSQL",
            subject_refs=[subject_ref],
        )
        assert decision.id == decision_id
        assert decision.domain == "database"
        assert decision.tier == "short_term"
        assert decision.status == "current"
        assert decision.outcome_truncated == "Chose PostgreSQL"
        assert len(decision.subject_refs) == 1


class TestTransition:
    """Test Transition dataclass."""

    def test_transition_creation(self) -> None:
        """Transition has required fields."""
        now = datetime.now(UTC)
        decision_id = uuid4()
        transition_id = uuid4()
        actor = Actor(kind="human", id="alice")
        transition = Transition(
            transition_id=transition_id,
            decision_id=decision_id,
            action="recorded",
            actor=actor,
            at=now,
            reason="Initial recording",
            new_status="current",
            payload={},
        )
        assert transition.transition_id == transition_id
        assert transition.decision_id == decision_id
        assert transition.action == "recorded"
        assert transition.actor == actor
        assert transition.at == now
        assert transition.reason == "Initial recording"
        assert transition.new_status == "current"
        assert transition.payload == {}


class TestLink:
    """Test Link dataclass."""

    def test_link_creation(self) -> None:
        """Link has from_id, to_id, and kind fields."""
        from_id = uuid4()
        to_id = uuid4()
        link = Link(from_id=from_id, to_id=to_id, kind="SUPERSEDES")
        assert link.from_id == from_id
        assert link.to_id == to_id
        assert link.kind == "SUPERSEDES"


class TestQueueItem:
    """Test QueueItem dataclass."""

    def test_queue_item_creation(self) -> None:
        """QueueItem has required fields."""
        now = datetime.now(UTC)
        item_id = uuid4()
        decision_id = uuid4()
        target_id = uuid4()
        actor = Actor(kind="human", id="alice")
        item = QueueItem(
            item_id=item_id,
            kind="promote",
            decision_id=decision_id,
            target_id=target_id,
            proposed_by=actor,
            proposed_at=now,
            rationale="Ready for production",
            confidence=0.9,
            resolved=False,
        )
        assert item.item_id == item_id
        assert item.kind == "promote"
        assert item.decision_id == decision_id
        assert item.target_id == target_id
        assert item.proposed_by == actor
        assert item.proposed_at == now
        assert item.rationale == "Ready for production"
        assert item.confidence == 0.9
        assert item.resolved is False


class TestSuggestion:
    """Test Suggestion dataclass."""

    def test_suggestion_creation(self) -> None:
        """Suggestion has kind, rationale, and confidence fields."""
        suggestion = Suggestion(
            kind="supersedes",
            rationale="New decision makes this one obsolete",
            confidence=0.85,
        )
        assert suggestion.kind == "supersedes"
        assert suggestion.rationale == "New decision makes this one obsolete"
        assert suggestion.confidence == 0.85


class TestCandidatePair:
    """Test CandidatePair dataclass."""

    def test_candidate_pair_creation(self) -> None:
        """CandidatePair has decision, other, and similarity fields."""
        decision_id1 = uuid4()
        decision_id2 = uuid4()
        decision = CompactDecision(
            id=decision_id1,
            domain="database",
            tier="short_term",
            status="current",
            outcome_truncated="Chose PostgreSQL",
            subject_refs=[],
        )
        other = CompactDecision(
            id=decision_id2,
            domain="database",
            tier="short_term",
            status="current",
            outcome_truncated="Chose MySQL",
            subject_refs=[],
        )
        pair = CandidatePair(decision=decision, other=other, similarity=0.92)
        assert pair.decision == decision
        assert pair.other == other
        assert pair.similarity == 0.92


class TestPromotionAssessment:
    """Test PromotionAssessment dataclass."""

    def test_promotion_assessment_creation(self) -> None:
        """PromotionAssessment has decision_id, recommend, rationale, confidence fields."""
        decision_id = uuid4()
        assessment = PromotionAssessment(
            decision_id=decision_id,
            recommend=True,
            rationale="Meets all criteria",
            confidence=0.92,
        )
        assert assessment.decision_id == decision_id
        assert assessment.recommend is True
        assert assessment.rationale == "Meets all criteria"
        assert assessment.confidence == 0.92


class TestFrozenImmutability:
    """Test that all frozen dataclasses are immutable."""

    @pytest.mark.parametrize(
        "obj,attr",
        [
            (Actor(kind="human", id="alice"), "id"),
            (
                CompactDecision(
                    id=uuid4(),
                    domain="test",
                    tier="short_term",
                    status="current",
                    outcome_truncated="test",
                    subject_refs=[],
                ),
                "domain",
            ),
            (
                Transition(
                    transition_id=uuid4(),
                    decision_id=uuid4(),
                    action="recorded",
                    actor=Actor(kind="human", id="alice"),
                    at=datetime.now(UTC),
                    reason="test",
                    new_status="current",
                    payload={},
                ),
                "reason",
            ),
            (Link(from_id=uuid4(), to_id=uuid4(), kind="SUPERSEDES"), "kind"),
            (
                QueueItem(
                    item_id=uuid4(),
                    kind="promote",
                    decision_id=uuid4(),
                    target_id=uuid4(),
                    proposed_by=Actor(kind="human", id="alice"),
                    proposed_at=datetime.now(UTC),
                    rationale="test",
                    confidence=0.5,
                    resolved=False,
                ),
                "resolved",
            ),
            (
                CandidatePair(
                    decision=CompactDecision(
                        id=uuid4(),
                        domain="test",
                        tier="short_term",
                        status="current",
                        outcome_truncated="test",
                        subject_refs=[],
                    ),
                    other=CompactDecision(
                        id=uuid4(),
                        domain="test",
                        tier="short_term",
                        status="current",
                        outcome_truncated="test",
                        subject_refs=[],
                    ),
                    similarity=0.5,
                ),
                "similarity",
            ),
            (
                Suggestion(kind="supersedes", rationale="test", confidence=0.5),
                "kind",
            ),
            (
                PromotionAssessment(
                    decision_id=uuid4(),
                    recommend=True,
                    rationale="test",
                    confidence=0.5,
                ),
                "recommend",
            ),
        ],
    )
    def test_frozen_dataclass_immutable(self, obj: object, attr: str) -> None:
        """All frozen dataclasses reject attribute assignment."""
        with pytest.raises((AttributeError, ValueError)):
            setattr(obj, attr, None)  # type: ignore
