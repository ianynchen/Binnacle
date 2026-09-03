"""JSON export shaping (REQUIREMENTS FR-6.6; docs/components/04-query-and-assist.md
"export()"): `store.export_rows()` returns an `ExportBundle` of typed dataclasses
(`UUID`, `datetime`, `Actor`) -- this module's only job is turning that into the
JSON-safe dict `Binnacle.export()` hands callers directly, `json.dumps`-able with
no further conversion on the caller's side.

Same free-function-over-a-domain-value shape as `application.query.precedent`
(no state to hold between calls), but simpler still: pure shaping, no I/O and no
port. Embeddings are never part of `ExportBundle` in the first place
(`store.export_rows` already excludes them, FR-6.6, "derived, rebuildable") --
nothing here re-excludes anything.
"""

from datetime import UTC, datetime
from typing import Any

from binnacle.domain.models import (
    Actor,
    Decision,
    DomainRecord,
    ExportBundle,
    Link,
    OptionConsidered,
    Ref,
    Transition,
)


def to_json(bundle: ExportBundle) -> dict[str, Any]:
    """Shape `bundle` into the JSON-safe export document: `UUID` -> `str`,
    `datetime` -> ISO-8601 UTC, `Actor` -> `"kind:id"`, every dataclass -> a
    plain dict. `schema_version` is `bundle.schema_version` (the export
    document's own shape version, FR-6.6) -- distinct from each decision's own
    `schema_version` (the stored row's versioning), which travels along inside
    that decision's dict unchanged.
    """
    return {
        "schema_version": bundle.schema_version,
        "decisions": [_decision_json(d) for d in bundle.decisions],
        "links": [_link_json(link) for link in bundle.links],
        "transitions": [_transition_json(t) for t in bundle.transitions],
        "domains": [_domain_json(d) for d in bundle.domains],
    }


def _actor_json(actor: Actor) -> str:
    return actor.as_str()


def _dt_json(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt is not None else None


def _ref_json(ref: Ref) -> dict[str, Any]:
    return {"role": ref.role, "kind": ref.kind, "identifier": ref.identifier, "note": ref.note}


def _option_json(opt: OptionConsidered) -> dict[str, Any]:
    return {"option": opt.option, "why_rejected": opt.why_rejected}


def _decision_json(d: Decision) -> dict[str, Any]:
    return {
        "decision_id": str(d.decision_id),
        "domain": d.domain,
        "tier": d.tier,
        "status": d.status,
        "scenario": d.scenario,
        "outcome": d.outcome,
        "reasoning": d.reasoning,
        "source": d.source,
        "recorded_by": _actor_json(d.recorded_by),
        "recorded_at": _dt_json(d.recorded_at),
        "decided_at": _dt_json(d.decided_at),
        "options_considered": [_option_json(o) for o in d.options_considered],
        "consequences": d.consequences,
        "confidence": d.confidence,
        "valid_from": _dt_json(d.valid_from),
        "valid_until": _dt_json(d.valid_until),
        "refs": [_ref_json(r) for r in d.refs],
        "supersedes": [str(u) for u in d.supersedes],
        "supplements": [str(u) for u in d.supplements],
        "metadata": d.metadata,
        "schema_version": d.schema_version,
    }


def _link_json(link: Link) -> dict[str, Any]:
    return {"from_id": str(link.from_id), "to_id": str(link.to_id), "kind": link.kind}


def _transition_json(t: Transition) -> dict[str, Any]:
    return {
        "transition_id": t.transition_id,
        "decision_id": str(t.decision_id),
        "action": t.action,
        "actor": _actor_json(t.actor),
        "at": _dt_json(t.at),
        "reason": t.reason,
        "new_status": t.new_status,
        "payload": t.payload,
    }


def _domain_json(d: DomainRecord) -> dict[str, Any]:
    return {"name": d.name, "description": d.description, "active": d.active}
