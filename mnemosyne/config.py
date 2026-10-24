"""Configuration: resolve where the store lives and which embedding provider
to use. Reads (in order of precedence) explicit args -> env vars -> config.toml
-> defaults. Everything has a working default so it runs with zero setup."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# Trust tiers (who may write what is enforced from Phase 2 on; Phase 1 records
# them so every write is attributable to a known writer):
#   0 untrusted · 1 standard · 2 senior · 3 owner
TIER_UNTRUSTED, TIER_STANDARD, TIER_SENIOR, TIER_OWNER = 0, 1, 2, 3


@dataclass
class WriterSpec:
    tier: int = TIER_STANDARD
    teams: List[str] = field(default_factory=list)   # team rings it belongs to


def _default_writers() -> Dict[str, WriterSpec]:
    # single-user default: the owner is the only writer
    return {"owner": WriterSpec(tier=TIER_OWNER)}


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

    # identity
    writer: str = "owner"             # writer id for local (stdio/CLI) use;
                                      # over HTTP the bearer token decides
    writers: Dict[str, WriterSpec] = field(default_factory=_default_writers)

    # HTTP transport
    http_host: str = "127.0.0.1"
    http_port: int = 8787

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
    def journal_dir(self) -> Path:
        return self.root / "journal"

    @property
    def db_path(self) -> Path:
        return self.index_dir / "memory.db"

    def tier_of(self, writer: str) -> int:
        """A writer's trust tier. Unknown writers are untrusted (fail closed)."""
        spec = self.writers.get(writer)
        return spec.tier if spec else TIER_UNTRUSTED

    def ensure_dirs(self) -> None:
        for p in (self.notes_dir, self.archive_dir, self.index_dir, self.journal_dir):
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

    if "writers" in data:
        cfg.writers = _parse_writers(data["writers"])
    cfg.writer = os.environ.get("MNEMOSYNE_WRITER", cfg.writer).strip() or cfg.writer

    http = data.get("http", {})
    cfg.http_host = str(http.get("host", cfg.http_host))
    cfg.http_port = int(http.get("port", cfg.http_port))

    cfg.ensure_dirs()
    return cfg


def _parse_writers(raw: Dict[str, Any]) -> Dict[str, WriterSpec]:
    """`[writers]` maps writer id -> `{ tier = N }` (or the shorthand `id = N`).
    Tokens are never stored here; they live in MNEMOSYNE_TOKEN_<WRITER> env vars."""
    out: Dict[str, WriterSpec] = {}
    for writer, spec in raw.items():
        if isinstance(spec, int):
            spec = {"tier": spec}
        if not isinstance(spec, dict):
            raise ValueError(f"[writers] {writer}: expected a table like {{ tier = 1 }}")
        tier = int(spec.get("tier", TIER_STANDARD))
        if not TIER_UNTRUSTED <= tier <= TIER_OWNER:
            raise ValueError(f"[writers] {writer}: tier must be 0-3, got {tier}")
        teams = spec.get("teams", [])
        if isinstance(teams, str):
            teams = [teams]
        out[writer] = WriterSpec(tier=tier, teams=[str(t).strip().lower() for t in teams])
    return out
