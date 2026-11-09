-- Partial replacement between decisions (REQUIREMENTS FR-3.1a, ADR 0002, added
-- 2026-11-08). `PARTIALLY_SUPERSEDES` is the second replacement relation: it
-- replaces SOME of the target's rulings and leaves the rest in force, so it
-- changes no status and carries prose naming the part it replaced.
--
-- `part` is the first and only payload column on `links` — every other kind is
-- fully described by (from_id, kind, to_id). The CHECK makes the pairing
-- structural rather than conventional: a partial-replacement link with no part
-- is the one thing FR-3.1a refuses outright ("carries a `part` description ...
-- and is refused without one"), and a part on any other kind would be a claim
-- that kind cannot make. Enforced here, not only in the Lifecycle Engine,
-- because a part-less partial link asserts "some ruling of this decision was
-- replaced, and nothing says which" — unreadable by the human it exists for,
-- and no application-side guard can speak for rows written by a repair script
-- or a future writer.
--
-- Existing rows need no backfill: every one of them predates this kind, so
-- `kind <> 'PARTIALLY_SUPERSEDES'` holds for all of them and NULL `part` is
-- exactly what the CHECK requires.

ALTER TABLE {schema}.links ADD COLUMN part TEXT;

ALTER TABLE {schema}.links ADD CONSTRAINT links_part_iff_partial
  CHECK ((kind = 'PARTIALLY_SUPERSEDES') = (part IS NOT NULL));
