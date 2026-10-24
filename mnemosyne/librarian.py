"""The librarian cycle: curation as a staffed organizational function.

This is the reference implementation of the loop in LIBRARIAN.md. Discovery
is deterministic (`engine.curate()`); *judgment* is delegated to a `Judge`,
which in production is an LLM (e.g. a scheduled Claude Code session acting as
writer `librarian`, tier 2) and in tests is a simple rule-based stand-in.

One cycle:
  1. merge duplicate clusters   judge.merge -> memory_update the survivor,
                                memory_forget the rest
  2. contested pairs            judge.resolve_contradiction ->
                                  a resolution (winner supersedes loser) is
                                  *proposed* as a decision for owner sign-off
                                  (its `supersedes` edge is inert until
                                  promoted), or, if undecidable, an explicit
                                  `contradicts` edge so recall flags both
  3. review queue               judge.review -> promote clean facts, reject
                                noise with a reason (never its own proposals)
  4. weekly digest              judge.digest -> a canon `semantic` memory
                                tagged digest:<ISO-week> (updated in place if
                                this week's digest already exists)

The librarian never resolves a contradiction on its own authority and never
promotes its own proposals: those are the owner's calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Protocol, Tuple

from .engine import DIGEST_TAG, MemoryEngine
from .schema import Memory

LIBRARIAN = "librarian"


class Judge(Protocol):
    def merge(self, members: List[Memory]) -> str:
        """One statement that preserves everything true in the cluster."""

    def resolve_contradiction(self, a: Memory, b: Memory
                              ) -> Optional[Tuple[str, str, str]]:
        """(winner_id, loser_id, rationale), or None if it can't be decided
        from what is on file."""

    def review(self, proposal: Memory) -> Tuple[str, str]:
        """('promote' | 'reject' | 'skip', reason)."""

    def digest(self, week: str, work: Dict[str, object]) -> str:
        """The week's digest: decisions, resolutions, open questions."""


@dataclass
class CycleReport:
    merged: List[str] = field(default_factory=list)          # survivor ids
    forgotten: List[str] = field(default_factory=list)
    resolutions_proposed: List[str] = field(default_factory=list)
    contradictions_recorded: List[Tuple[str, str]] = field(default_factory=list)
    promoted: List[str] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)          # with reasons
    digest_id: Optional[str] = None


def run_cycle(eng: MemoryEngine, judge: Judge, writer: str = LIBRARIAN) -> CycleReport:
    rep = CycleReport()
    work = eng.curate(writer=writer)

    # 1. duplicates
    for cluster in work["duplicate_clusters"]:
        mems = [m for m in (eng.get(x["id"]) for x in cluster["members"]) if m]
        if len(mems) < 2:
            continue
        try:
            eng.update(mems[0].id, text=judge.merge(mems), writer=writer)
            for m in mems[1:]:
                eng.forget(m.id, writer=writer)
                rep.forgotten.append(m.id)
            rep.merged.append(mems[0].id)
        except PermissionError as e:
            rep.skipped.append(f"cluster {mems[0].id}: {e}")

    # 2. contradictions: propose, never decide
    for pair in work["contested"]:
        a, b = eng.get(pair["a"]), eng.get(pair["b"])
        if not a or not b:
            continue
        verdict = judge.resolve_contradiction(a, b)
        if verdict:
            winner, loser, why = verdict
            prop = eng.propose(
                text=f"Resolution: memory {winner} supersedes {loser}. {why}",
                type="decision", target="canon", writer=writer,
                entities=sorted(set(a.entities) | set(b.entities)),
                relations=[{"rel": "supersedes", "id": loser, "note": why}])
            rep.resolutions_proposed.append(prop.id)
        else:
            try:
                eng.relate(a.id, "contradicts", b.id, writer=writer,
                           note="librarian: undecidable from the record; needs the owner")
                rep.contradictions_recorded.append((a.id, b.id))
            except PermissionError as e:
                rep.skipped.append(f"contested {a.id}/{b.id}: {e}")

    # 3. review queue (fresh: it may have grown in step 2)
    for prop in eng.pending_proposals():
        if prop.proposed_by == writer:
            continue  # its own proposals wait for someone else
        action, reason = judge.review(prop)
        try:
            if action == "promote":
                eng.promote(prop.id, writer=writer)
                rep.promoted.append(prop.id)
            elif action == "reject":
                eng.reject(prop.id, reason, writer=writer)
                rep.rejected.append(prop.id)
            else:
                rep.skipped.append(f"proposal {prop.id}: {reason}")
        except PermissionError as e:   # e.g. a policy: the owner's call
            rep.skipped.append(f"proposal {prop.id}: {e}")

    # 4. digest, from the activity *after* this cycle's own work
    since = (date.today() - timedelta(days=7)).isoformat()
    work["activity_last_7_days"] = eng.activity(since)
    text = judge.digest(work["week"], work)
    tag = f"{DIGEST_TAG}{work['week']}"
    existing = work["digest"]["existing_id"]
    if existing and eng.get(existing):
        eng.update(existing, text=text, writer=writer)
        rep.digest_id = existing
    else:
        rep.digest_id = eng.remember(text, type="semantic", tags=[tag, "digest"],
                                     ring="canon", source="librarian",
                                     confidence=0.8, writer=writer).id
    return rep
