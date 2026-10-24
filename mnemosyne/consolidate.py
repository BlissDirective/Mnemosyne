"""reflect(): the process that makes memory improve over time.

Deterministic and conservative by design — it never silently rewrites your
words. It does three safe things:

  1. Dedup      near-identical memories of the same type are linked, the newer
                marked `duplicate_of` the older, and the survivor's confidence
                reinforced (more evidence -> higher confidence). Superseded
                memories are filtered out of recall by default.
  2. Reinforce  a memory corroborated by close neighbors gains confidence.
  3. Decay      (optional) stale, unlinked memories lose a little confidence
                each pass — but only if they were neither updated NOR recalled
                within `decay_min_age_days`. Access counters make this real:
                a memory the agent keeps using never decays, however old.

Every canonical write here is mirrored into the index via `update_meta` — the
text is unchanged so no re-embedding happens, but confidence, links and
duplicate_of must not go stale (they feed both recall filtering and the next
reflect pass).

Similarity is computed in row blocks rather than one n x n matrix, so memory
use stays flat as the store grows into the tens of thousands.

LLM-assisted merging (actually rewriting two memories into one better one) is
the natural next layer: the agent calls `recall`, sees a duplicate cluster in
the report, composes a merged memory, and writes it back with `memory_update`
(or `remember` + `forget`). This module surfaces those clusters so that loop
is easy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .schema import Memory
from .store import MarkdownStore
from .index import MemoryIndex

_SIM_BLOCK = 512  # rows per block when computing pairwise similarities


@dataclass
class ReflectReport:
    duplicate_clusters: List[List[str]] = field(default_factory=list)
    reinforced: List[str] = field(default_factory=list)
    decayed: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, object]:
        return {
            "duplicate_clusters": self.duplicate_clusters,
            "reinforced": self.reinforced,
            "decayed": self.decayed,
            "notes": self.notes,
        }


def _write_through(store: MarkdownStore, index: MemoryIndex, mem: Memory) -> None:
    """Persist a metadata-only change to BOTH layers. The canonical file is the
    source of truth; update_meta keeps the derived index from going stale
    without re-embedding (the text did not change)."""
    mem.touch()
    store.write(mem)
    index.update_meta(mem)


# persist hook: the engine passes one that also journals the change
Persist = Callable[[Memory], None]


def _pairs_above(matrix: np.ndarray, threshold: float) -> List[Tuple[int, int, float]]:
    """All (i, j, sim) with i < j and cosine >= threshold, computed block-wise so
    peak memory is O(block * n) instead of O(n^2)."""
    n = matrix.shape[0]
    out: List[Tuple[int, int, float]] = []
    for start in range(0, n, _SIM_BLOCK):
        end = min(start + _SIM_BLOCK, n)
        block = matrix[start:end] @ matrix.T          # (block, n)
        ii, jj = np.nonzero(block >= threshold)
        for bi, j in zip(ii, jj):
            i = start + int(bi)
            j = int(j)
            if i < j:
                out.append((i, j, float(block[bi, j])))
    return out


def _neighbor_counts(matrix: np.ndarray, threshold: float) -> np.ndarray:
    """Per-row count of *other* rows with cosine >= threshold, block-wise."""
    n = matrix.shape[0]
    counts = np.zeros(n, dtype=np.int64)
    for start in range(0, n, _SIM_BLOCK):
        end = min(start + _SIM_BLOCK, n)
        block = matrix[start:end] @ matrix.T
        counts[start:end] = np.sum(block >= threshold, axis=1) - 1  # exclude self
    return counts


def reflect(store: MarkdownStore, index: MemoryIndex,
            dedup_threshold: float = 0.94,
            decay: float = 0.0,
            decay_min_age_days: int = 30,
            dry_run: bool = False,
            persist: Optional[Persist] = None) -> ReflectReport:
    report = ReflectReport()
    if persist is None:
        def persist(mem: Memory) -> None:
            _write_through(store, index, mem)
    ids, matrix = index.all_vectors()
    if matrix.shape[0] < 2:
        report.notes.append("Not enough memories to consolidate yet.")
        return report

    # id -> (type, confidence, created) for grouping/updating
    meta: Dict[str, Tuple[str, float, str]] = {}
    for mid in ids:
        row = index.get_meta(mid)
        if row:
            meta[mid] = (row["type"], float(row["confidence"] or 0.7), row["created"])

    n = len(ids)
    parent = list(range(n))  # union-find for clustering duplicates

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    reinforced_set = set()
    for i, j, _sim in _pairs_above(matrix, dedup_threshold):
        ti = meta.get(ids[i], ("", 0, ""))[0]
        tj = meta.get(ids[j], ("", 0, ""))[0]
        if ti != tj:
            continue  # only merge like-with-like
        union(i, j)
        reinforced_set.add(ids[i])
        reinforced_set.add(ids[j])

    # build clusters
    clusters: Dict[int, List[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    for members in clusters.values():
        if len(members) < 2:
            continue
        # earliest-created memory is the survivor (id tie-break for determinism —
        # ids are sortable timestamps, and same-second creations are common)
        members_sorted = sorted(members, key=lambda idx: (meta[ids[idx]][2], ids[idx]))
        survivor_id = ids[members_sorted[0]]
        cluster_ids = [ids[m] for m in members_sorted]
        report.duplicate_clusters.append(cluster_ids)

        if dry_run:
            continue
        # reinforce survivor confidence; mark the rest as duplicates + link
        surv = store.read_by_id(survivor_id)
        if surv is None:
            continue
        boost = min(0.99, surv.confidence + 0.05 * (len(members) - 1))
        surv.confidence = round(boost, 3)
        for dup_id in cluster_ids[1:]:
            if dup_id not in surv.links:
                surv.links.append(dup_id)
        persist(surv)
        report.reinforced.append(survivor_id)

        for dup_id in cluster_ids[1:]:
            dup = store.read_by_id(dup_id)
            if dup and dup.duplicate_of != survivor_id:
                dup.duplicate_of = survivor_id
                if survivor_id not in dup.links:
                    dup.links.append(survivor_id)
                persist(dup)

    # reinforce (non-duplicate) close neighbors: corroboration without merging
    if not dry_run:
        counts = _neighbor_counts(matrix, dedup_threshold - 0.1)
        for i in range(n):
            mid = ids[i]
            if mid in report.reinforced or mid in reinforced_set:
                continue
            if counts[i] >= 1:
                m = store.read_by_id(mid)
                if m and m.confidence < 0.95:
                    m.confidence = round(min(0.95, m.confidence + 0.02 * int(counts[i])), 3)
                    persist(m)
                    report.reinforced.append(mid)

    # optional decay — access-gated: only stale (neither updated nor recalled
    # within decay_min_age_days), unlinked memories lose confidence. Memories
    # the agent actually uses are left alone no matter how old they are.
    if decay > 0 and not dry_run:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=decay_min_age_days))
        cutoff_iso = cutoff.replace(microsecond=0).isoformat()
        for i in range(n):
            m = store.read_by_id(ids[i])
            if m is None or m.links or m.duplicate_of or m.confidence <= 0.2:
                continue
            last_activity = max(m.updated or "", m.last_recalled or "")
            if last_activity < cutoff_iso:
                m.confidence = round(max(0.2, m.confidence - decay), 3)
                persist(m)
                report.decayed.append(ids[i])

    return report
