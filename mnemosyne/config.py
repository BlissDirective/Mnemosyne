"""Configuration: resolve where the store lives and which embedding provider
to use. Reads (in order of precedence) explicit args -> env vars -> config.toml
-> defaults. Everything has a working default so it runs with zero setup."""

from __future__ import annotations

import hashlib
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
    broad_k: int = 10                 # a query with no entities and k >= this is
                                      # "broad": librarian digests are recalled first
    digest_k: int = 3                 # max digests placed ahead of raw memories

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
    contradiction_low: float = 0.70   # reflect flags same-type memories that share an
    contradiction_high: float = 0.94  # entity and sit in this cosine band (related,
                                      # not duplicates) as candidate contradictions

    # identity
    writer: str = "owner"             # writer id for local (stdio/CLI) use;
                                      # over HTTP the bearer token decides
    writers: Dict[str, WriterSpec] = field(default_factory=_default_writers)

    # guardrails (Phase 7): memories carrying any of these tags gate actions —
    # see guardrails.py and memory_recall_for_decision
    high_stakes_tags: List[str] = field(default_factory=lambda: [
        "payment", "contract", "pricing", "legal", "customer-commitment"])
    approval_hours: int = 24          # how long an owner approval (memory_approve) lasts

    # storage backend: "files" (Markdown + SQLite on this machine) or
    # "postgres" (one database, many workspaces; see pg.py). The connection
    # string comes ONLY from the MNEMOSYNE_DATABASE_URL env var.
    storage: str = field(default_factory=lambda: os.environ.get("MNEMOSYNE_STORAGE", "files"))
    workspace: str = field(default_factory=lambda: os.environ.get("MNEMOSYNE_WORKSPACE", "default"))
    pg_schema: str = field(default_factory=lambda: os.environ.get("MNEMOSYNE_PG_SCHEMA", "mnemosyne"))
    exact_search_max: int = 20000     # below this many vectors per workspace, semantic
                                      # search is exact; above, it uses the HNSW index
    database_url: str = field(default_factory=lambda: os.environ.get("MNEMOSYNE_DATABASE_URL", ""),
                              repr=False)

    # HTTP transport
    http_host: str = "127.0.0.1"
    http_port: int = 8787

    def __post_init__(self) -> None:
        self.resolve_workspace()

    def resolve_workspace(self) -> None:
        """workspace "@root" = one workspace per memory home (derived from the
        root path) — handy for tests and for one-store-per-directory setups."""
        if self.workspace == "@root":
            digest = hashlib.sha256(str(Path(self.root).resolve()).encode()).hexdigest()
            self.workspace = "ws_" + digest[:16]

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
    cfg.broad_k = int(ret.get("broad_k", cfg.broad_k))
    cfg.digest_k = int(ret.get("digest_k", cfg.digest_k))

    acc = data.get("access", {})
    cfg.counter_flush_every = max(1, int(acc.get("flush_every", cfg.counter_flush_every)))

    con = data.get("consolidation", {})
    cfg.dedup_threshold = float(con.get("dedup_threshold", cfg.dedup_threshold))
    cfg.decay_per_reflect = float(con.get("decay_per_reflect", cfg.decay_per_reflect))
    cfg.decay_min_age_days = int(con.get("decay_min_age_days", cfg.decay_min_age_days))
    band = con.get("contradiction_band", [cfg.contradiction_low, cfg.contradiction_high])
    cfg.contradiction_low, cfg.contradiction_high = float(band[0]), float(band[1])

    if "writers" in data:
        cfg.writers = _parse_writers(data["writers"])
    cfg.writer = os.environ.get("MNEMOSYNE_WRITER", cfg.writer).strip() or cfg.writer

    gr = data.get("guardrails", {})
    if "high_stakes_tags" in gr:
        cfg.high_stakes_tags = [str(t).strip().lower() for t in gr["high_stakes_tags"]]
    cfg.approval_hours = int(gr.get("approval_hours", cfg.approval_hours))

    st = data.get("storage", {})
    if "database_url" in st:
        raise ValueError("[storage] database_url must not be in config.toml (it holds a "
                         "password): set MNEMOSYNE_DATABASE_URL in the environment")
    cfg.storage = os.environ.get("MNEMOSYNE_STORAGE", st.get("backend", cfg.storage))
    cfg.workspace = os.environ.get("MNEMOSYNE_WORKSPACE", st.get("workspace", cfg.workspace))
    cfg.pg_schema = os.environ.get("MNEMOSYNE_PG_SCHEMA", st.get("schema", cfg.pg_schema))
    cfg.exact_search_max = int(st.get("exact_search_max", cfg.exact_search_max))
    cfg.resolve_workspace()

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
