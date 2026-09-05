"""The narrative end-to-end test (REQUIREMENTS §7 "The Life of a Decision" --
authoritative text; docs/binnacle-core/components/04-query-and-assist.md's export/precedent
contracts). One decision's whole life, told through the PUBLIC `Binnacle`
client only (no store/engine reach-ins) -- record -> same-session short-term
supersede -> an engine AND an agent recommendation -> the human gate
(promote_refined, and a sibling decline) -> the durable long-term life
(supplement, then supersede by a human) -> old age (`archive_stale`) ->
who-uses-decisions reads (precedent's dead history, the audit trail).

Every section below is headed by the §7 sentence it encodes and follows with
the assertions that sentence demands. Where FR-4.6's own example ("one
service's retry decision becomes policy for all remote calls... jitter added
to the backoff") sits ON TOP OF §7's plainer "you tap promote" -- §7.3 never
says the gate must be verbatim, and FR-4.6 is the very mechanism a human
"tapping promote" invokes when authoring at the gate -- this test exercises
`promote_refined` there, per the task brief's explicit direction. No other
place in this file diverges from §7's text.

`archival_age_days=0` on the fixture's config is a deliberate test-only knob,
not a store/engine reach-in: it collapses FR-3.4's "90 days untouched" and
FR-7.2's "aging unrecommended" clocks down to "before this statement" without
faking `recorded_at` (which the public API never exposes for writes -- FR-1.7
keeps it system-set). Every decision this test creates is accounted for by
name in the "old age" section below, so the collapsed clock cannot silently
sweep something the narrative didn't mean to age out.

Two `discover()` sub-contracts already have dedicated, exhaustive coverage in
tests/db/test_sweeps.py (the FR-7.4 O(k) bound, cursor resume, dedup, cap) and
are NOT re-proven here: this file exercises only the promotion-recommendation
half the decision's own story needs (an engine-nominated decision alongside
the agent-nominated one), not the relationship-suggestion half.
"""

from datetime import UTC, datetime, timedelta

from binnacle_core.application.config import BinnacleConfig
from binnacle_core.client import Binnacle
from binnacle_core.domain.models import (
    Actor,
    NewDecision,
    OptionConsidered,
    PromotionAssessment,
    Ref,
)
from tests.helpers import ScriptedSuggester, StubEmbedder

AGENT = Actor("agent", "meridian/sess-1")
HUMAN = Actor("human", "morgan")
ENGINE = Actor("engine", "binnacle")

DOMAIN = "architecture"
SUBJECT = ("component", "portolan-ingest")


class TestLifeOfADecision:
    async def test_the_life_of_a_decision(self, pg_dsn: str, scratch_schema: str) -> None:
        suggester = ScriptedSuggester()
        config = BinnacleConfig(
            dsn=pg_dsn,
            schema_name=scratch_schema,
            embedder=StubEmbedder(dim=16),
            embedding_dim=16,
            suggester=suggester,
            archival_age_days=0,
        )
        bn = Binnacle(config)
        await bn.migrate()
        await bn.add_domain(DOMAIN, "architecture decisions", actor=HUMAN)

        # -- 7.1 The moment of recording -----------------------------------
        # "An agent inside a meridian workflow, mid-task, settles something...
        # It records a decision." MUST: domain, scenario, outcome, reasoning,
        # source, actor. SHOULD: options_considered, a subject ref, an
        # evidence ref, a confidence.
        backoff = await bn.record(
            NewDecision(
                domain=DOMAIN,
                scenario="how should transient ingestion failures be handled?",
                outcome="retries use exponential backoff, capped at 3 attempts",
                reasoning="bounded retries avoid unbounded resource consumption on persistent failures",
                source="meridian",
                options_considered=[
                    OptionConsidered(
                        option="fixed-interval retry", why_rejected="thundering herd on recovery"
                    )
                ],
                refs=[
                    Ref(role="subject", kind=SUBJECT[0], identifier=SUBJECT[1], note=None),
                    Ref(role="evidence", kind="session", identifier="sess-1", note=None),
                ],
                confidence=0.8,
            ),
            actor=AGENT,
        )
        # "What lands in storage, atomically: one row in `decisions`
        # (tier=short, status=current), its `refs` rows, and a `recorded`
        # transition carrying actor and timestamp."
        assert backoff.tier == "short_term"
        assert backoff.status == "current"
        assert {(r.role, r.kind, r.identifier) for r in backoff.refs} == {
            ("subject", *SUBJECT),
            ("evidence", "session", "sess-1"),
        }
        h_backoff = await bn.history(backoff.decision_id)
        assert [t.action for t in h_backoff.transitions] == ["recorded"]
        assert h_backoff.transitions[0].actor == AGENT

        # "The decision is immediately findable by domain/subject/status
        # queries; its embedding is computed asynchronously minutes later...
        # Nothing about recording waited on an LLM, an embedding call, or a
        # human." -- no backfill/discover has run yet, and it's already here.
        findable = await bn.relevant(domains=[DOMAIN], subject=SUBJECT)
        assert any(d.id == backoff.decision_id for d in findable.items)  # type: ignore[union-attr]

        # -- 7.2 The working life (short-term tier) --------------------------
        # "Later that session the approach changes -- batching makes retries
        # unnecessary. The agent records the new decision declaring
        # `supersedes` on the old one: the old row stays, status=superseded,
        # linked to its successor; the working record now tells the truth
        # about both the path and the destination."
        batching = await bn.record(
            NewDecision(
                domain=DOMAIN,
                scenario="how should transient ingestion failures be handled?",
                outcome="batch ingestion writes so retries are unnecessary",
                reasoning="batching absorbs transient failures without a retry loop at all",
                source="meridian",
                refs=[Ref(role="subject", kind=SUBJECT[0], identifier=SUBJECT[1], note=None)],
                supersedes=[backoff.decision_id],
            ),
            actor=AGENT,
        )
        h_backoff = await bn.history(backoff.decision_id)
        assert h_backoff.decision.status == "superseded"
        assert any(
            link.kind == "SUPERSEDES"
            and link.from_id == batching.decision_id
            and link.to_id == backoff.decision_id
            for link in h_backoff.links
        )
        old_side = next(t for t in h_backoff.transitions if t.action == "superseded")
        assert old_side.actor == AGENT  # short-term <-> short-term is ungated (FR-5.2)
        assert old_side.payload == {"target": str(batching.decision_id)}
        h_batching = await bn.history(batching.decision_id)
        assert [t.action for t in h_batching.transitions] == ["recorded", "superseded"]
        assert h_batching.decision.status == "current"  # superseding doesn't change its OWN status
        assert h_batching.transitions[1].payload == {"target": str(backoff.decision_id)}

        # "At workflow end, the agent files a promotion recommendation on the
        # surviving decision ('this is standing policy for ingestion, not
        # session detail') -- a queue item, nothing more."
        agent_item_id = await bn.recommend(
            batching.decision_id,
            actor=AGENT,
            reason="this is standing policy for ingestion, not session detail",
        )
        assert agent_item_id is not None

        # "A malformed duplicate it accidentally logged gets `discarded` (its
        # own recording, its own session -- allowed), hidden from every
        # default view, deleted from none."
        dup = await bn.record(
            NewDecision(
                domain=DOMAIN,
                scenario="how should transient ingestion failures be handled?",
                outcome="retries use exponential backoff, capped at 3 attempts",
                reasoning="accidental duplicate log entry",
                source="meridian",
            ),
            actor=AGENT,
        )
        await bn.discard(dup.decision_id, actor=AGENT, reason="malformed duplicate")
        h_dup = await bn.history(dup.decision_id)
        assert h_dup.decision.status == "discarded"
        default_view = await bn.relevant(domains=[DOMAIN])
        assert dup.decision_id not in {d.id for d in default_view.items}  # type: ignore[union-attr]
        assert dup.decision_id in {d.decision_id for d in await bn.get_many([dup.decision_id])}

        # "Meanwhile the nightly sweep (a meridian job)... also nominates
        # aging short-term decisions that smell like policy. Its output is
        # queue items and only queue items." -- an engine-side recommendation
        # (FR-7.2), independent of the agent's own recommendation above,
        # exercised on a SEPARATE decision the agent never got around to
        # recommending (`aging_unrecommended` skips `batching`, which already
        # has an open item).
        pool_sizing = await bn.record(
            NewDecision(
                domain=DOMAIN,
                scenario="how should the ingestion connection pool be sized under load?",
                outcome="size the pool at 2x expected peak concurrency",
                reasoning="avoids connection exhaustion without over-provisioning",
                source="meridian",
            ),
            actor=AGENT,
        )
        suggester.queue_promotion_assessment(
            PromotionAssessment(
                decision_id=pool_sizing.decision_id,
                recommend=True,
                rationale="stable ingestion policy, ready for promotion",
                confidence=0.85,
            )
        )
        discovery_summary = await bn.discover(batch=100)
        assert discovery_summary.promotions_recommended == 1
        h_pool = await bn.history(pool_sizing.decision_id)
        engine_recommended = [t for t in h_pool.transitions if t.action == "recommended"]
        assert len(engine_recommended) == 1
        assert engine_recommended[0].actor == ENGINE
        open_promotes = await bn.queue(kinds=["promote"])
        pool_item = next(
            v for v in open_promotes.items if v.item.decision_id == pool_sizing.decision_id
        )
        assert pool_item.item.proposed_by == ENGINE

        # -- 7.3 The gate -----------------------------------------------------
        # "For the backoff decision you tap promote. In one transaction: a
        # long-term copy is created (PROMOTED_FROM link back to the source)...
        # the short-term source flips to `promoted`, the queue item resolves,
        # and every step lands as a transition under your name." -- authored
        # here per FR-4.6's own example (module docstring): generalized
        # subject (no refs -> applies to all remote calls, not just
        # portolan-ingest) and an amended outcome (jitter added).
        refined = await bn.promote_refined(
            [batching.decision_id],
            refined=NewDecision(
                domain=DOMAIN,
                scenario="standardize retry backoff across all remote calls",
                outcome="all remote calls use exponential backoff with jitter, capped at 3 attempts",
                reasoning=(
                    "jitter avoids synchronized retry storms across services; "
                    "the policy generalizes beyond ingestion"
                ),
                source="meridian",
            ),
            actor=HUMAN,
        )
        assert refined.tier == "long_term"
        assert refined.status == "current"
        assert refined.recorded_by == HUMAN
        h_batching = await bn.history(batching.decision_id)
        assert h_batching.decision.status == "promoted"
        assert any(
            link.kind == "PROMOTED_FROM"
            and link.from_id == refined.decision_id
            and link.to_id == batching.decision_id
            for link in h_batching.links
        )
        promoted_t = next(t for t in h_batching.transitions if t.action == "promoted")
        assert promoted_t.actor == HUMAN
        assert promoted_t.payload == {"refined": True, "target": str(refined.decision_id)}
        queue_after_gate = await bn.queue(kinds=["promote"])
        assert not any(v.item.decision_id == batching.decision_id for v in queue_after_gate.items)
        # FR-4.6's generalization made it through: the refined policy is
        # unscoped, so a subject dossier for the ORIGINAL component still
        # surfaces it (FR-6.1: subject match OR unscoped).
        dossier = await bn.relevant(domains=[DOMAIN], subject=SUBJECT, tier="long_term")
        assert any(d.id == refined.decision_id for d in dossier.items)  # type: ignore[union-attr]

        # "For another item -- an agent's 'we should use tabs not spaces' --
        # you tap decline with reason: not_promoted, kept as signal,
        # re-recommendable if it ever stops being noise."
        tabs = await bn.record(
            NewDecision(
                domain=DOMAIN,
                scenario="should the repo standardize on tabs or spaces?",
                outcome="use tabs for indentation",
                reasoning="agent's own preference, not backed by team consensus",
                source="meridian",
            ),
            actor=AGENT,
        )
        tabs_item_id = await bn.recommend(
            tabs.decision_id, actor=AGENT, reason="proposed style guideline"
        )
        assert tabs_item_id is not None
        await bn.decline(tabs_item_id, actor=HUMAN, reason="style bikeshedding, not a real policy")
        h_tabs = await bn.history(tabs.decision_id)
        assert h_tabs.decision.status == "not_promoted"
        declined_t = next(t for t in h_tabs.transitions if t.action == "declined")
        assert declined_t.actor == HUMAN

        # "When *you* make a deliberate durable decision yourself, you skip
        # the queue: direct long-term recording, one atomic act, still fully
        # transitioned."
        dead_lettering = await bn.record_long_term(
            NewDecision(
                domain=DOMAIN,
                scenario="how should queue-fed ingestion handle repeated failures?",
                outcome="queue-fed ingestion additionally uses dead-lettering",
                reasoning=(
                    "a dead-letter queue captures poison messages the backoff/jitter "
                    "policy alone can't resolve"
                ),
                source="meridian",
            ),
            actor=HUMAN,
        )
        assert dead_lettering.tier == "long_term"
        h_dl = await bn.history(dead_lettering.decision_id)
        assert [t.action for t in h_dl.transitions] == ["recorded", "promoted"]

        # -- 7.4 The durable life (long-term tier) ---------------------------
        # "Then the platform adopts a message queue and a new decision
        # `supplements` it ('backoff stands; queue-fed ingestion additionally
        # uses dead-lettering') -- the original stays `current`, readers see
        # it with its supplement alongside."
        await bn.supplement(dead_lettering.decision_id, refined.decision_id, actor=HUMAN)
        h_refined = await bn.history(refined.decision_id)
        assert h_refined.decision.status == "current"  # supplemented is not a status (FR-5.3)
        assert dead_lettering.decision_id in {d.decision_id for d in h_refined.supplements}
        supplement_link = next(link for link in h_refined.links if link.kind == "SUPPLEMENTS")
        assert supplement_link.from_id == dead_lettering.decision_id
        assert supplement_link.to_id == refined.decision_id

        # "A temporary waiver recorded with `valid_until` simply expires by
        # clock -- no ceremony."
        just_expired = datetime.now(UTC) - timedelta(seconds=1)
        waiver = await bn.record(
            NewDecision(
                domain=DOMAIN,
                scenario="short-term deploy freeze during the migration window",
                outcome="temporary waiver: skip the standard backoff cap during the migration",
                reasoning="migration traffic is atypical; revert automatically once the window closes",
                source="meridian",
                valid_until=just_expired,
            ),
            actor=AGENT,
        )
        no_ceremony = await bn.relevant(domains=[DOMAIN])
        assert waiver.decision_id not in {d.id for d in no_ceremony.items}  # type: ignore[union-attr]
        # no status transition was involved -- purely a read-time filter.
        h_waiver = await bn.history(waiver.decision_id)
        assert h_waiver.decision.status == "current"
        # an `as_of` from before it expired still finds it (the window was real).
        before_expiry = just_expired - timedelta(hours=1)
        as_of_before_expiry = await bn.relevant(domains=[DOMAIN], as_of=before_expiry)
        assert waiver.decision_id in {d.id for d in as_of_before_expiry.items}  # type: ignore[union-attr]

        # "A year on, a redesign `supersedes` it outright: new decision,
        # link, `superseded` status -- executed by a human, because every
        # long-term mutation is. It never vanishes... with the original
        # reasoning, evidence, and the whole transition history intact."
        mesh_retry = await bn.record_long_term(
            NewDecision(
                domain=DOMAIN,
                scenario="how should remote-call failures be handled after the service-mesh migration?",
                outcome=(
                    "remote calls rely on the service mesh's built-in retry and circuit "
                    "breaking; app-level backoff is removed"
                ),
                reasoning="the mesh sidecar now owns retry policy uniformly; duplicating it drifts",
                source="meridian",
            ),
            actor=HUMAN,
        )
        await bn.supersede(mesh_retry.decision_id, refined.decision_id, actor=HUMAN)
        h_refined = await bn.history(refined.decision_id)
        assert h_refined.decision.status == "superseded"
        assert any(
            link.kind == "SUPERSEDES"
            and link.from_id == mesh_retry.decision_id
            and link.to_id == refined.decision_id
            for link in h_refined.links
        )
        new_side = next(t for t in h_refined.transitions if t.action == "superseded")
        assert new_side.actor == HUMAN
        assert h_refined.decision.reasoning == refined.reasoning  # immutable, never edited in place
        h_backoff = await bn.history(
            backoff.decision_id
        )  # "why did we ever do backoff?" answerable
        assert h_backoff.decision.reasoning == backoff.reasoning

        # -- 7.5 Old age --------------------------------------------------------
        # "Short-term decisions nobody touched for 90 days -- never
        # recommended, or declined and never revisited -- are `archived` by
        # the clock sweep: out of default queries, the queue, and the hot
        # indexes; still there under `include_archived`; instantly revivable
        # by a re-recommendation." `archival_age_days=0` (module docstring)
        # makes every untouched short-term `current`/`not_promoted` decision
        # eligible immediately: `tabs` (declined, item resolved) and `waiver`
        # (current, never recommended) qualify; every other short-term
        # decision is terminal (superseded/promoted/discarded) or blocked by
        # an open item (`pool_sizing`, per FR-3.4).
        archive_summary = await bn.archive_stale()
        assert archive_summary.archived == 2
        h_tabs = await bn.history(tabs.decision_id)
        assert h_tabs.decision.status == "archived"
        h_waiver = await bn.history(waiver.decision_id)
        assert h_waiver.decision.status == "archived"
        h_pool = await bn.history(pool_sizing.decision_id)
        assert h_pool.decision.status == "current"  # its open queue item stopped the clock

        after_archival = await bn.relevant(domains=[DOMAIN])
        assert tabs.decision_id not in {d.id for d in after_archival.items}  # type: ignore[union-attr]
        assert waiver.decision_id not in {d.id for d in after_archival.items}  # type: ignore[union-attr]
        with_archived = await bn.relevant(
            domains=[DOMAIN],
            status=["current", "not_promoted", "superseded"],
            include_archived=True,
        )
        assert tabs.decision_id in {d.id for d in with_archived.items}  # type: ignore[union-attr]
        assert tabs.decision_id in {d.decision_id for d in await bn.get_many([tabs.decision_id])}

        revive_item_id = await bn.recommend(
            tabs.decision_id, actor=HUMAN, reason="reconsidering after a team RFC"
        )
        assert revive_item_id is not None
        h_tabs = await bn.history(tabs.decision_id)
        assert h_tabs.decision.status == "not_promoted"  # restored to its pre-archive status
        assert any(t.action == "reactivated" for t in h_tabs.transitions)

        # -- 7.6 Who uses decisions, and how -------------------------------
        # "Agents, working: before proposing a design, a precedent check
        # ('prior decisions about retries?') returns the current backoff
        # policy *and* its superseded ancestor -- how the thinking evolved is
        # part of the answer." Embeddings are backfilled here, minutes late,
        # exactly as §7.1 describes -- nothing above waited on this.
        await bn.backfill_embeddings(batch=100)
        hits = await bn.precedent(
            "how should retry backoff be configured for ingestion?", domains=[DOMAIN], limit=50
        )
        status_by_id = {h.decision.id: h.decision.status for h in hits}
        assert status_by_id[backoff.decision_id] == "superseded"  # dead history, present + labeled
        assert status_by_id[refined.decision_id] == "superseded"  # the LT ancestor too
        assert mesh_retry.decision_id in status_by_id  # the current policy
        assert status_by_id[tabs.decision_id] == "not_promoted"  # revived -> visible again
        # "The record itself" -- archived/discarded are gone, not history:
        assert waiver.decision_id not in status_by_id  # still archived, never revived
        assert dup.decision_id not in status_by_id  # discarded

        # "You, curating: ... the audit view when something looks off
        # ('everything `agent:meridian/*` promoted -- wait, agents can't
        # promote; prove it' -- the transition log does)."
        agent_promotions = await bn.changes(actions=["promoted"], actor=AGENT)
        assert agent_promotions == []
        all_promotions = await bn.changes(actions=["promoted"])
        assert any(t.decision_id == batching.decision_id for t, _ in all_promotions)

        await bn.aclose()
