"""Memory rings: who can read and write which part of the organization's memory.

    private:<writer>   an agent's own working notebook
    team:<name>        shared by a team (e.g. team:sales)
    proposed           the review queue: quarantined until a senior promotes it
    canon              reviewed organizational truth

The filesystem mirrors the ring (notes/canon/, notes/proposed/,
notes/team/<name>/, notes/private/<writer>/), so the owner can see the whole
topology with `ls`. The ring itself lives in each memory's frontmatter, which
stays the source of truth.

Every access rule lives in this module, so the policy can be read in one place:

  write   private:<self>   anyone
          private:<other>  tier 3 only
          proposed         anyone (it is a queue, not truth)
          team:<t>         tier >= 2; tier 1 if a member of <t>; else QUARANTINED
          canon            tier >= 2; else QUARANTINED
  read    canon            everyone
          private:<self>   self; another writer's private ring needs tier 3
          team:<t>         members, or tier >= 2
          proposed         tier >= 2 (reviewers)

"Quarantined" means the write is not refused: it lands in `proposed` with the
intended ring recorded, and becomes shared truth only when a *different*
writer of tier >= 2 promotes it. Low-trust writers therefore physically cannot
pollute shared recall.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import List, Optional, Sequence, Set, Tuple

from .auth import Identity
from .config import TIER_OWNER, TIER_SENIOR, TIER_STANDARD

CANON = "canon"
PROPOSED = "proposed"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def private(writer: str) -> str:
    return f"private:{writer}"


def team(name: str) -> str:
    return f"team:{name}"


def parse(ring: str) -> Tuple[str, Optional[str]]:
    """'team:sales' -> ('team', 'sales'); 'canon' -> ('canon', None).
    Raises ValueError (with the accepted forms) on anything else."""
    r = (ring or "").strip().lower()
    if r in (CANON, PROPOSED):
        return r, None
    kind, _, name = r.partition(":")
    if kind in ("team", "private") and _NAME_RE.match(name):
        return kind, name
    raise ValueError(f"invalid ring {ring!r}: use 'canon', 'proposed', "
                     "'team:<name>' or 'private:<writer>'")


def normalize(ring: str) -> str:
    kind, name = parse(ring)
    return f"{kind}:{name}" if name else kind


def ring_dir(ring: str) -> PurePosixPath:
    """Directory (relative to notes/) that holds a ring's files."""
    kind, name = parse(ring)
    return PurePosixPath(kind, name) if name else PurePosixPath(kind)


def is_shared_target(ring: str) -> bool:
    """Rings a proposal may be promoted into."""
    return parse(ring)[0] in (CANON, "team")


# ---- write policy -----------------------------------------------------------
def write_decision(ident: Identity, target: Optional[str]) -> Tuple[str, bool]:
    """Where a write by `ident` aimed at `target` actually lands.
    Returns (ring, quarantined). Raises PermissionError if refused outright."""
    ring = normalize(target) if target else private(ident.writer)
    kind, name = parse(ring)
    if kind == "private":
        if name == ident.writer or ident.tier >= TIER_OWNER:
            return ring, False
        raise PermissionError(
            f"{ident.writer} (tier {ident.tier}) cannot write {ring}: another "
            "writer's private ring needs tier 3. Write to your own ring or "
            "'proposed' instead.")
    if kind == PROPOSED:
        return ring, False
    if can_write_directly(ident, ring):
        return ring, False
    return PROPOSED, True


def can_write_directly(ident: Identity, ring: str) -> bool:
    kind, name = parse(ring)
    if kind == "private":
        return name == ident.writer or ident.tier >= TIER_OWNER
    if kind == PROPOSED:
        return True
    if kind == "team":
        return ident.tier >= TIER_SENIOR or (
            ident.tier >= TIER_STANDARD and name in ident.teams)
    return ident.tier >= TIER_SENIOR  # canon


def check_can_modify(ident: Identity, mem) -> None:
    """Updating or forgetting an existing memory needs direct write access to
    its ring (no quarantine path: you cannot quietly edit shared truth). A
    proposal can be edited by its proposer or a reviewer."""
    if mem.ring == PROPOSED:
        if ident.writer == mem.proposed_by or ident.tier >= TIER_SENIOR:
            return
    elif can_write_directly(ident, mem.ring):
        return
    raise PermissionError(
        f"{ident.writer} (tier {ident.tier}) cannot modify memory {mem.id} in "
        f"{mem.ring}. To suggest a change, call memory_propose with the id and "
        "the corrected text; a senior writer will review it.")


# ---- read policy ------------------------------------------------------------
def can_read(ident: Identity, ring: str) -> bool:
    kind, name = parse(ring)
    if kind == CANON:
        return True
    if kind == "private":
        return name == ident.writer or ident.tier >= TIER_OWNER
    if kind == "team":
        return name in ident.teams or ident.tier >= TIER_SENIOR
    return ident.tier >= TIER_SENIOR  # proposed


def default_scope(ident: Identity) -> List[str]:
    """canon + the caller's own private ring + their teams."""
    return [CANON, private(ident.writer)] + [team(t) for t in ident.teams]


def resolve_scope(ident: Identity, scope: Optional[Sequence[str]],
                  known_rings: Sequence[str] = ()) -> Set[str]:
    """The concrete set of rings a recall may search. `None`/empty means the
    default scope; 'all' expands to every known ring the caller may read.
    Asking for a ring you cannot read is an error, not a silent drop."""
    if not scope:
        return set(default_scope(ident))
    out: Set[str] = set()
    for item in scope:
        if item.strip().lower() == "all":
            out |= {r for r in set(known_rings) | set(default_scope(ident))
                    if can_read(ident, r)}
            continue
        ring = normalize(item)
        if not can_read(ident, ring):
            raise PermissionError(
                f"{ident.writer} (tier {ident.tier}) cannot read {ring}. "
                "Readable: canon, your own private ring, your teams"
                + ("" if ident.tier < TIER_SENIOR else ", any team, proposed") + ".")
        out.add(ring)
    return out
