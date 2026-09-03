"""Binnacle: a PostgreSQL-backed decision-record library.

Deliberately narrow public surface (docs/components/01-configuration-and-client.md
"Everything else in the package is reachable only through this surface"): the
client, its config, the domain vocabulary callers construct/receive, the typed
error hierarchy, and the ports a caller fulfills.
"""

from binnacle.application.config import BinnacleConfig, DiscoveryConfig
from binnacle.application.ports import Embedder, Suggester
from binnacle.client import Binnacle
from binnacle.domain.errors import (
    AuthorityViolation,
    BinnacleError,
    ConfigError,
    DecisionNotFound,
    EmbeddingDimensionMismatch,
    IdempotencyConflict,
    InactiveDomain,
    InvalidTransition,
    ItemAlreadyResolved,
    ItemNotFound,
    UnknownDomain,
)
from binnacle.domain.models import (
    Actor,
    CandidatePair,
    CompactDecision,
    Decision,
    DomainRecord,
    ExportBundle,
    HistoryRecord,
    Link,
    NewDecision,
    OptionConsidered,
    PromotionAssessment,
    QueueItem,
    QueueItemView,
    Ref,
    Suggestion,
    Transition,
)

__all__ = [
    "Actor",
    "AuthorityViolation",
    "Binnacle",
    "BinnacleConfig",
    "BinnacleError",
    "CandidatePair",
    "CompactDecision",
    "ConfigError",
    "Decision",
    "DecisionNotFound",
    "DiscoveryConfig",
    "DomainRecord",
    "Embedder",
    "EmbeddingDimensionMismatch",
    "ExportBundle",
    "HistoryRecord",
    "IdempotencyConflict",
    "InactiveDomain",
    "InvalidTransition",
    "ItemAlreadyResolved",
    "ItemNotFound",
    "Link",
    "NewDecision",
    "OptionConsidered",
    "PromotionAssessment",
    "QueueItem",
    "QueueItemView",
    "Ref",
    "Suggester",
    "Suggestion",
    "Transition",
    "UnknownDomain",
]
