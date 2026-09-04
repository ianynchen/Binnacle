"""Binnacle domain errors."""


class BinnacleError(Exception):
    """Root exception for all Binnacle domain errors."""


class ConfigError(BinnacleError):
    """Configuration error."""


class UnknownDomain(BinnacleError):
    """Unknown or unregistered domain."""


class InactiveDomain(BinnacleError):
    """Domain is registered but deactivated; recording into it is refused."""


class DecisionNotFound(BinnacleError):
    """Decision record not found."""


class InvalidTransition(BinnacleError):
    """Invalid state transition attempted."""

    def __init__(self, from_status: str, attempted_action: str, message: str = "") -> None:
        """Initialize with status and action context.

        Args:
            from_status: The current status.
            attempted_action: The action that was attempted.
            message: Optional additional message.
        """
        self.from_status = from_status
        self.attempted_action = attempted_action
        full_message = f"Invalid transition from {from_status} via {attempted_action}"
        if message:
            full_message += f": {message}"
        super().__init__(full_message)


class AuthorityViolation(BinnacleError):
    """Actor lacks authority for this operation."""


class IdempotencyConflict(BinnacleError):
    """Idempotency key conflict."""


class EmbeddingDimensionMismatch(BinnacleError):
    """Embedding dimension mismatch."""


class ItemNotFound(BinnacleError):
    """Item not found."""


class ItemAlreadyResolved(BinnacleError):
    """Item is already resolved."""


class InvalidResolution(BinnacleError):
    """A queue-item resolution call's arguments are malformed -- e.g.
    `resolve_conflict`'s `winner_id`/`refined`/`reason` combination names zero
    or more than one resolution path, or `winner_id` isn't one of the item's
    two decisions. Distinct from `InvalidTransition`, which is reserved for
    state-related illegality (wrong status, wrong item kind, a cycle) -- this
    is pure caller argument misuse, decidable before any row is even locked.
    """
