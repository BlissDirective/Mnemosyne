"""Mnemosyne — shared, governed memory for teams of AI agents, from any vendor.

Design in one sentence: the *source of truth* is plain Markdown you own (plus
a hash-chained journal of every change); the searchable index (vectors +
keyword + graph) is a rebuildable cache derived from it.

Layers:
  store        canonical Markdown + YAML frontmatter, one directory per ring
  journal      hash-chained event log: replay, as-of snapshots, verification
  embeddings   pluggable providers (dev / OpenAI / Voyage / local)
  index        SQLite (WAL, metadata + FTS5 + relations) + numpy vectors
  retrieval    multi-strategy recall fused with weighted RRF
  consolidate  reflect(): dedup, reinforce, decay, contradiction candidates
  auth, rings  writer identity, trust tiers, ring access policy, quarantine
  reputation   outcome-calibrated writer reputation
  librarian    the curation cycle and weekly digests
  guardrails   secrets screen and the high-stakes decision gate
  metrics      pilot metrics from the journal
  engine       the single entry point; server (MCP stdio/HTTP), gateway
               (JSON/HTTP) and cli are thin surfaces over it
"""

__version__ = "1.0.0"

from .schema import Memory, MemoryType  # noqa: E402,F401

__all__ = ["Memory", "MemoryType", "__version__"]
