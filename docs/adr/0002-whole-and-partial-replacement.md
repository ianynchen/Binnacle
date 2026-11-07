# ADR 0002: Whole and partial replacement between decisions

Status: Accepted
Date: 2026-11-07

## Context

Binnacle's model has one replacement relation. `supersede` moves a decision's
status to `superseded` and links it to its successor (FR-3.2, FR-5.2), and that
status means the decision is no longer in force.

Surveying the fleet's own 17 decision records to migrate them into Binnacle
showed that **every supersession relationship between them is partial, and none
is whole.** Three instances, in the records' own words:

- Meridian `DEC-0003`: *"Supersedes DEC-0001's projection ruling. DEC-0001's
  framing of Binnacle as the system of record for decisions stands unchanged."*
- Portolan `0002`: *"Supersedes decision 0001's inclusion of `Claim` in the v1
  object set; the remaining 0001 objects … stand."*
- Portolan `0004`: *"Supersedes decision 0001's projection expectation and closes
  decision 0002's deferred `decision` category item."*

Recording any of these as `supersede` asserts something false: that the whole
decision has fallen. Meridian `DEC-0001` would be marked dead while the framing
it is most cited for explicitly stands, and every query for decisions touching
`component:binnacle` would then exclude it.

## Decision

Define two distinct relations, from primitives. No definition below uses
"supersede" to define itself.

### Primitives

A **ruling** is one position a decision takes on one question. A decision
usually contains several.

A ruling is **in force** when no later decision has taken a different position
on the same question.

To **replace** a ruling is for a later decision to take a different position on
the question that ruling answered.

### The relations

**Whole replacement.** Decision B *wholly replaces* decision A when B replaces
every ruling in A. Afterwards no ruling in A is in force; A is historical.

**Partial replacement.** Decision B *partially replaces* decision A when B
replaces some rulings in A and leaves the others in force. A remains the
authority for everything B did not touch, and stays `current`.

### Consequences for status

`superseded` means **every ruling in this decision has been replaced**. Only
whole replacement sets it.

A decision that has only ever been partially replaced stays `current`. This is
not leniency: it remains the authority for its untouched rulings, and marking it
`superseded` would both state a falsehood and remove it from every
current-status query.

### What a system can and cannot know

Neither Binnacle nor a lint can read rulings, so neither can decide which kind of
replacement a link is. The claim belongs to the decision doing the replacing,
and the two are distinguished by exactly one thing: whether the replacing
decision names the part it replaced. That `part` is prose for a human to read;
nothing parses it. The structured fact is the link and its kind.

### Consequences for precedent and recall

Citing a wholly-replaced decision means nothing relied upon is in force.

Citing a partially-replaced decision means one of its rulings is gone and the
system **cannot tell** whether it was the one relied upon. That is a question for
a human, and a consumer surfacing it must say so rather than implying the
citation is broken.

## Status of the implementation

**Binnacle does not implement partial replacement yet.** `supersede` is
whole-only, and the long-term tier's statuses are `current | superseded`
(FR-3.2). This ADR defines the relation; closing the gap is a change to
binnacle-core, and it is a **precondition for ingesting the fleet's own decision
records**, because all three of their relationships are partial and none can be
recorded truthfully today.

Minimum shape of that change, when it is made:

- A partial-replacement link kind alongside the existing supersession link,
  carrying a `part` description.
- `supersede`'s invariant unchanged: it still means whole, and still moves status.
- A partial link leaves the target's status alone, and the state machine must
  refuse to treat it as a transition.
- Reads distinguish the two, so a caller can ask "is anything here still in
  force?" and get a true answer.

## Consequences

- Statuses stop being able to mean "partly out of date", which they never could
  express anyway — they now say so explicitly rather than by omission.
- The 17 fleet records become recordable without asserting falsehoods.
- Meridian's decision lint implements the same two relations over front matter,
  which is where the definition is first enforced; it cites this ADR.
- Portolan's `SUPERSEDES` relationship type has the same whole-only limitation
  for documents and entities. Whether a document can partially replace another is
  a separate question, deliberately not settled here.

## Revisit trigger

If a case appears where a human cannot say whether a replacement was whole or
partial, the claim-based distinction is insufficient and the relation needs a
third kind rather than a sharper rule.
