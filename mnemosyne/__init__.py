"""Mnemosyne — a local-first, portable memory engine for AI agents.

Design in one sentence: the *source of truth* is plain Markdown you own; the
searchable index (vectors + keyword + graph) is a rebuildable cache derived
from it. Swap embedding models freely and re-index from the preserved text —
that is the "evolutionary" property.

Layers:
  store        canonical Markdown + YAML frontmatter (+ verbatim archive)
  embeddings   pluggable providers (dev / OpenAI / Voyage / local),
               batched + retried, with asymmetric query embeddings
  index        SQLite (WAL, metadata + FTS5 keyword) + numpy vectors
  retrieval    multi-strategy recall fused with weighted Reciprocal Rank
               Fusion; filters superseded duplicates and expired facts
  consolidate  reflect(): dedup, reinforce, decay — memory that refines itself
               (decay is gated on real access: recently recalled memories
               never decay)
  engine       update-in-place, access counters with batched write-back,
               and `prime` context for session-start hooks
  server       FastMCP server exposing remember / recall / update / reflect /
               forget / stats / reindex
"""

__version__ = "0.6.0"

from .schema import Memory, MemoryType  # noqa: E402,F401

__all__ = ["Memory", "MemoryType", "__version__"]
