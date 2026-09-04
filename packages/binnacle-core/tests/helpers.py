"""Test-only port fulfillments (ARCHITECTURE §3.1: "Development/test fulfillment:
a deterministic stub embedder and a scripted suggester") — never shipped in
`src/`, since core stays LLM-free (FR-7.1) and binnacle never constructs one of
these itself.
"""

import hashlib

from binnacle_core.domain.models import (
    CandidatePair,
    CompactDecision,
    PromotionAssessment,
    Suggestion,
)


class StubEmbedder:
    """A deterministic `Embedder` (ARCHITECTURE §3.1): each text hashes to a
    fixed-dimension unit-ish vector, so the same text always embeds to the same
    vector (useful for exercising idempotency/backfill/precedent tests without a
    real model) and different texts embed to different vectors."""

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vector: list[float] = []
        seed = text.encode("utf-8")
        counter = 0
        while len(vector) < self.dim:
            digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for i in range(0, len(digest), 4):
                if len(vector) >= self.dim:
                    break
                chunk = digest[i : i + 4]
                # Map an unsigned 32-bit chunk to a small signed float, keeping
                # every component's magnitude comparable (no dominating axis).
                value = int.from_bytes(chunk, "big") / 0xFFFFFFFF
                vector.append(value * 2.0 - 1.0)
            counter += 1
        return vector[: self.dim]


class ScriptedSuggester:
    """A `Suggester` (ARCHITECTURE §3.1) whose answers are pre-scripted by the
    test, rather than inferred — `classify_pairs`/`assess_promotion` each pop
    from a caller-supplied queue, keyed by call order. Exists for Task 8's
    discovery-sweep tests; Task 6 exports it here (not shipped in `src/`) since
    it belongs alongside `StubEmbedder` as the pair of test-only port
    fulfillments named in ARCHITECTURE §3.1.
    """

    def __init__(
        self,
        pair_suggestions: list[Suggestion] | None = None,
        promotion_assessments: list[PromotionAssessment] | None = None,
    ) -> None:
        self._pair_suggestions = list(pair_suggestions or [])
        self._promotion_assessments = list(promotion_assessments or [])
        self.classify_pairs_calls: list[list[CandidatePair]] = []
        self.assess_promotion_calls: list[list[CompactDecision]] = []

    async def classify_pairs(self, pairs: list[CandidatePair]) -> list[Suggestion]:
        self.classify_pairs_calls.append(pairs)
        taken, self._pair_suggestions = (
            self._pair_suggestions[: len(pairs)],
            self._pair_suggestions[len(pairs) :],
        )
        return taken

    async def assess_promotion(self, decisions: list[CompactDecision]) -> list[PromotionAssessment]:
        self.assess_promotion_calls.append(decisions)
        taken, self._promotion_assessments = (
            self._promotion_assessments[: len(decisions)],
            self._promotion_assessments[len(decisions) :],
        )
        return taken

    def queue_promotion_assessment(self, assessment: PromotionAssessment) -> None:
        """Script one more `assess_promotion` answer after construction --
        for callers (e.g. a narrative-style test) that only learn a
        decision's id from an earlier `record()` call, so the assessment
        can't be known at `ScriptedSuggester(...)` construction time."""
        self._promotion_assessments.append(assessment)
