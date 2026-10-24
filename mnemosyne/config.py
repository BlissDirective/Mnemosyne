"""Configuration: resolve where the store lives and which embedding provider
to use. Reads (in order of precedence) explicit args -> env vars -> config.toml
-> defaults. Everything has a working default so it runs with zero setup."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


def _default_root() -> Path:
    # Canonical memory lives here by default; override with MNEMOSYNE_HOME.
    return Path(os.environ.get("MNEMOSYNE_HOME", "~/mnemosyne")).expanduser()


@dataclass
class Config:
    root: Path = field(default_factory=_default_root)
    # embedding provider: "dev" | "openai" | "voyage" | "local"
    provider: str = "dev"
    model: Optional[str] = None       # provider-specific; None -> provider default
    dim: Optional[int] = None         # optional override (e.g. OpenAI MRL truncation)

    # retrieval knobs
    recall_k: int = 8
    candidate_k: int = 40             # per-strategy candidate pool before fusion
    rrf_k: int = 60                   # Reciprocal Rank Fusion constant
    recency_weight: float = 0.3       # recency's RRF weight when it runs at all
                                      # (it only runs when the query has temporal
                                      # cues — see retrieval.py)

    # access counters
    counter_flush_every: int = 10     # write recall_count/last_recalled back to the
                                      # canonical files after this many recorded
                                      # recall hits (1 = every recall). Batching
                                      # keeps Git diffs quiet; counters also flush
                                      # on close/reflect/reindex.

    # consolidation knobs
    dedup_threshold: float = 0.94     # cosine >= this (same type) => near-duplicate
    decay_per_reflect: float = 0.0    # confidence decay for stale, unlinked memories
    decay_min_age_days: int = 30      # a memory is decay-eligible only if it was
                                      # neither updated NOR recalled in this window

    # ---- derived paths ---------------------------------------------------
    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    @property
    def index_dir(self) -> Path:
        return self.root / ".index"

    @property
    def db_path(self) -> Path:
        return self.index_dir / "memory.db"

    def ensure_dirs(self) -> None:
        for p in (self.notes_dir, self.archive_dir, self.index_dir):
            p.mkdir(parents=True, exist_ok=True)


def load_config(root: Optional[str] = None) -> Config:
    """Load config from <root>/config.toml if present, overlaying env + args."""
    base = Path(root).expanduser() if root else _default_root()
    cfg = Config(root=base)

    toml_path = base / "config.toml"
    data: Dict[str, Any] = {}
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

    emb = data.get("embedding", {})
    cfg.provider = os.environ.get("MNEMOSYNE_PROVIDER", emb.get("provider", cfg.provider))
    cfg.model = os.environ.get("MNEMOSYNE_MODEL", emb.get("model", cfg.model))
    dim = os.environ.get("MNEMOSYNE_DIM", emb.get("dim"))
    cfg.dim = int(dim) if dim else None

    ret = data.get("retrieval", {})
    cfg.recall_k = int(ret.get("recall_k", cfg.recall_k))
    cfg.candidate_k = int(ret.get("candidate_k", cfg.candidate_k))
    cfg.rrf_k = int(ret.get("rrf_k", cfg.rrf_k))
    cfg.recency_weight = float(ret.get("recency_weight", cfg.recency_weight))

    acc = data.get("access", {})
    cfg.counter_flush_every = max(1, int(acc.get("flush_every", cfg.counter_flush_every)))

    con = data.get("consolidation", {})
    cfg.dedup_threshold = float(con.get("dedup_threshold", cfg.dedup_threshold))
    cfg.decay_per_reflect = float(con.get("decay_per_reflect", cfg.decay_per_reflect))
    cfg.decay_min_age_days = int(con.get("decay_min_age_days", cfg.decay_min_age_days))

    cfg.ensure_dirs()
    return cfg
