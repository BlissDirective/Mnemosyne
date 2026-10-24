"""Recall: run several retrieval strategies in parallel and fuse them.

No single strategy is enough. Semantic search misses exact names and dates;
keyword search misses paraphrases; neither handles "what did we do last spring"
(temporal) or "everything about Alice" (graph). So we run several and merge the
ranked lists with *weighted* Reciprocal Rank Fusion (RRF).

Two things keep recall clean:

  * Recency is gated and down-weighted. It only participates when the query
    actually has temporal cues ("recent", "yesterday", "last week", ...), and
    even then at a reduced RRF weight — otherwise whatever was stored five
    minutes ago pollutes every unrelated query.
  * Superseded and expired memories are filtered. Anything reflect() marked
    `duplicate_of`, and any world_fact whose `valid_to` has passed, is dropped
    by default (pass include_superseded=True to see everything). Without this,
    consolidation links duplicates but recall keeps returning the noise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .schema import Memory
from .embeddings import EmbeddingProvider
from .index import MemoryIndex


@dataclass
class Hit:
    memory: Memory
    score: float
    strategies: List[str]
    # ids of live memories this one has an unresolved `contradicts` edge with
    contested_with: List[str] = field(default_factory=list)


# Queries containing these tokens/phrases are asking about *time*, so the
# recency strategy is allowed to contribute (at reduced weight).
_TEMPORAL_CUES = {
    "recent", "recently", "latest", "last", "yesterday", "today", "tonight",
    "lately", "newest", "just", "ago", "past", "current", "currently", "now",
    "this week", "this month", "this year", "these days",
}


def _has_temporal_cue(query: str) -> bool:
    q = query.lower()
    toks = set(re.findall(r"[a-z]+", q))
    if toks & {t for t in _TEMPORAL_CUES if " " not in t}:
        return True
    return any(phrase in q for phrase in _TEMPORAL_CUES if " " in phrase)


def _rrf_merge(ranked_lists: Dict[str, List[str]], k: int, rrf_k: int,
               weights: Optional[Dict[str, float]] = None,
               ) -> List[Tuple[str, float, List[str]]]:
    """Weighted Reciprocal Rank Fusion. Each id scores
    sum(weight[strategy] / (rrf_k + rank)) across the lists it appears in; we
    also track which strategies contributed."""
    weights = weights or {}
    scores: Dict[str, float] = {}
    hits: Dict[str, List[str]] = {}
    for strategy, ids in ranked_lists.items():
        w = weights.get(strategy, 1.0)
        if w <= 0:
            continue
        for rank, mid in enumerate(ids):
            scores[mid] = scores.get(mid, 0.0) + w / (rrf_k + rank)
            hits.setdefault(mid, []).append(strategy)
    fused = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(mid, sc, hits[mid]) for mid, sc in fused[:k]]


class Retriever:
    def __init__(self, store, index: MemoryIndex, provider: EmbeddingProvider,
                 candidate_k: int = 40, rrf_k: int = 60,
                 recency_weight: float = 0.3):
        self.store = store
        self.index = index
        self.provider = provider
        self.candidate_k = candidate_k
        self.rrf_k = rrf_k
        self.recency_weight = recency_weight

    def recall(self, query: str, k: int = 8,
               types: Optional[Sequence[str]] = None,
               entities: Optional[Sequence[str]] = None,
               use_recency: Optional[bool] = None,
               include_superseded: bool = False,
               rings: Optional[Sequence[str]] = None,
               superseded: Optional[Set[str]] = None,
               contested: Optional[Mapping[str, List[str]]] = None,
               prior: Optional[Callable[[Memory], float]] = None) -> List[Hit]:
        """`rings` restricts every strategy to those rings (None = no ring
        filter; the engine always passes the caller's resolved scope).
        `superseded` ids (targets of a live `supersedes` relation) are hidden
        like duplicates; `contested` maps id -> the ids it is contested with,
        and is attached to each hit so nobody acts on it unknowingly.
        `prior(memory)` is a bounded multiplier on each fused score (the
        engine's confidence term, nudged by writer reputation)."""
        superseded = superseded or set()
        contested = contested or {}
        ck = self.candidate_k
        ranked: Dict[str, List[str]] = {}

        # 1) semantic — queries go through embed_query so asymmetric providers
        #    (Voyage) embed them as queries, not documents
        try:
            qv = self.provider.embed_query(query)
            sem = self.index.semantic(qv, ck, types=types, rings=rings)
            ranked["semantic"] = [mid for mid, _ in sem]
        except Exception:
            ranked["semantic"] = []  # e.g. no API key -> degrade to keyword/graph

        # 2) keyword / BM25
        ranked["keyword"] = [mid for mid, _ in self.index.keyword(query, ck, rings=rings)]

        # 3) graph expansion (entities the caller flagged, or words in the query)
        ents = list(entities) if entities else _guess_entities(query)
        if ents:
            ranked["graph"] = [mid for mid, _ in self.index.graph(ents, ck, rings=rings)]

        # 4) temporal / recency prior — gated on temporal cues (or explicit
        #    use_recency=True), and down-weighted in fusion either way
        recency_on = _has_temporal_cue(query) if use_recency is None else use_recency
        if recency_on:
            ranked["recency"] = self.index.recent(min(ck, 15), types=types, rings=rings)

        # over-fetch so post-fusion filtering and re-weighting can still fill k
        pool = max(k * 3, k + 8)
        fused = _rrf_merge(ranked, k=pool, rrf_k=self.rrf_k,
                           weights={"recency": self.recency_weight})

        cands: List[Tuple[Memory, float, List[str]]] = []
        for mid, score, strategies in fused:
            mem = self.store.read_by_id(mid)
            if mem is None:
                continue
            cands.append((mem, score * (prior(mem) if prior else 1.0), strategies))
        cands.sort(key=lambda c: -c[1])

        hits: List[Hit] = []
        for mem, score, strategies in cands:
            if rings is not None and mem.ring not in rings:
                continue  # index lagging the notes: the canonical ring decides
            if not include_superseded:
                if mem.duplicate_of:      # merged into a survivor by reflect()
                    continue
                if mem.id in superseded:  # a reviewed memory supersedes it
                    continue
                if mem.is_retired():      # past its valid_to, or refuted
                    continue
            hits.append(Hit(memory=mem, score=score, strategies=strategies,
                            contested_with=list(contested.get(mem.id, []))))
            if len(hits) >= k:
                break
        return hits


# Light entity guess: capitalized tokens tend to be names/proper nouns — but a
# capital at the start of a sentence is usually just grammar. So sentence-
# initial tokens only count if the same word also appears capitalized
# mid-sentence somewhere in the query. The agent can (and should) pass explicit
# `entities` for precision.
def _guess_entities(query: str) -> List[str]:
    sentence_initial: List[str] = []
    mid_sentence: List[str] = []
    at_start = True
    for tok in query.replace(",", " ").split():
        w = tok.strip(".?!:;\"'()")
        is_candidate = len(w) > 2 and w[:1].isupper() and w.lower() not in _STOP
        if is_candidate:
            (sentence_initial if at_start else mid_sentence).append(w.lower())
        at_start = tok.rstrip()[-1:] in ".?!" if tok.strip() else at_start
    confirmed = set(mid_sentence)
    out = list(dict.fromkeys(mid_sentence))          # keep order, dedupe
    out += [w for w in sentence_initial if w in confirmed and w not in out]
    return out


_STOP = {"the", "what", "when", "where", "who", "why", "how", "did", "does",
         "and", "for", "our", "your", "with", "about", "this", "that"}
