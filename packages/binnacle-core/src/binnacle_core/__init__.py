"""Binnacle: a PostgreSQL-backed decision-record library.

Deliberately narrow public surface (docs/binnacle-core/components/01-configuration-and-client.md
"Everything else in the package is reachable only through this surface"): the
client, its config, the domain vocabulary callers construct/receive, the typed
error hierarchy, and the ports a caller fulfills.
"""

from binnacle_core.application.config import BinnacleConfig, DiscoveryConfig
from binnacle_core.application.ports import Embedder, Suggester
from binnacle_core.client import Binnacle
from binnacle_core.domain.errors import (
    AuthorityViolation,
    BinnacleError,
    ConfigError,
    DecisionNotFound,
    EmbeddingDimensionMismatch,
    IdempotencyConflict,
    InactiveDomain,
    InvalidResolution,
    InvalidTransition,
    ItemAlreadyResolved,
    ItemNotFound,
    UnknownDomain,
)
from binnacle_core.domain.models import (
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
    "InvalidResolution",
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
