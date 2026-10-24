"""The derived layer: a rebuildable index over the canonical Markdown store.

Backends, all in one SQLite file (no fragile extension loading):
  * metadata table         -> filters (type, time, entities, confidence)
  * FTS5 virtual table     -> keyword / BM25 search
  * vectors as BLOBs       -> loaded into a numpy matrix for cosine search
  * entity co-occurrence   -> lightweight graph expansion

Because it is fully derived from the notes, it can be deleted and rebuilt at
will, and re-embedded when the embedding model changes.

Concurrency: the MCP server may dispatch tools on worker threads, so the
connection is opened with check_same_thread=False, WAL journaling is enabled,
and *all* database access (reads included) is serialized behind one lock. A
local single-user store is low-contention; correctness beats parallel reads.

Sync: two independent change signals are tracked per row —
  content_hash  (text)      changed  -> re-embed + full upsert
  meta_hash     (metadata)  changed  -> cheap metadata refresh, vector reused
That second path is what keeps reflect()'s frontmatter edits (confidence,
links, duplicate_of) and the access counters from going permanently stale in
the index.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .schema import Memory, MemoryType
from .embeddings import EmbeddingProvider


SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    text          TEXT NOT NULL,
    created       TEXT NOT NULL,
    updated       TEXT NOT NULL,
    source        TEXT,
    confidence    REAL,
    valid_from    TEXT,
    valid_to      TEXT,
    tags          TEXT,          -- json array
    entities      TEXT,          -- json array
    duplicate_of  TEXT,          -- id of the surviving memory, if superseded
    recall_count  INTEGER DEFAULT 0,
    last_recalled TEXT,
    writer        TEXT,
    ring          TEXT,
    due           TEXT,
    review_by     TEXT,
    content_hash  TEXT,
    meta_hash     TEXT,
    embed_model   TEXT,
    dim           INTEGER,
    vector        BLOB
);
CREATE INDEX IF NOT EXISTS idx_mem_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_mem_created ON memories(created);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
    USING fts5(id UNINDEXED, text, tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS mem_entities (
    memory_id TEXT NOT NULL,
    entity    TEXT NOT NULL,
    PRIMARY KEY (memory_id, entity)
);
CREATE INDEX IF NOT EXISTS idx_ent ON mem_entities(entity);

-- typed edges, derived from each memory's `relations:` frontmatter
CREATE TABLE IF NOT EXISTS relations (
    src  TEXT NOT NULL,
    rel  TEXT NOT NULL,
    dst  TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (src, rel, dst)
);
CREATE INDEX IF NOT EXISTS idx_rel_dst ON relations(dst);
"""

# columns added since 0.1.0 — patched in on open so an old index keeps working
# (it is a cache, so `reindex` would also rebuild it from scratch just fine)
_MIGRATIONS = {
    "meta_hash": "ALTER TABLE memories ADD COLUMN meta_hash TEXT",
    "duplicate_of": "ALTER TABLE memories ADD COLUMN duplicate_of TEXT",
    "recall_count": "ALTER TABLE memories ADD COLUMN recall_count INTEGER DEFAULT 0",
    "last_recalled": "ALTER TABLE memories ADD COLUMN last_recalled TEXT",
    "writer": "ALTER TABLE memories ADD COLUMN writer TEXT",
    "ring": "ALTER TABLE memories ADD COLUMN ring TEXT",
    "due": "ALTER TABLE memories ADD COLUMN due TEXT",
    "review_by": "ALTER TABLE memories ADD COLUMN review_by TEXT",
}


def _vec_to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def _blob_to_vec(b: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32, count=dim)


class MemoryIndex:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            cols = {r["name"] for r in self.db.execute("PRAGMA table_info(memories)")}
            for col, ddl in _MIGRATIONS.items():
                if col not in cols:
                    self.db.execute(ddl)
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_mem_ring ON memories(ring)")
            self.db.commit()
        self._matrix: Optional[np.ndarray] = None   # cached (n, dim) vectors
        self._ids: List[str] = []                    # row order for the matrix
        self._matrix_dim: Optional[int] = None

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ---- write path ------------------------------------------------------
    def upsert(self, mem: Memory, vector: np.ndarray, embed_model: str) -> None:
        with self._lock:
            self._upsert_locked(mem, vector, embed_model)

    def _upsert_locked(self, mem: Memory, vector: np.ndarray, embed_model: str) -> None:
        dim = int(vector.shape[0])
        self.db.execute(
            """INSERT INTO memories
               (id,type,text,created,updated,source,confidence,valid_from,valid_to,
                tags,entities,duplicate_of,recall_count,last_recalled,writer,ring,
                due,review_by,content_hash,meta_hash,embed_model,dim,vector)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 type=excluded.type, text=excluded.text, updated=excluded.updated,
                 source=excluded.source, confidence=excluded.confidence,
                 valid_from=excluded.valid_from, valid_to=excluded.valid_to,
                 tags=excluded.tags, entities=excluded.entities,
                 duplicate_of=excluded.duplicate_of,
                 recall_count=excluded.recall_count, last_recalled=excluded.last_recalled,
                 writer=excluded.writer, ring=excluded.ring,
                 due=excluded.due, review_by=excluded.review_by,
                 content_hash=excluded.content_hash, meta_hash=excluded.meta_hash,
                 embed_model=excluded.embed_model,
                 dim=excluded.dim, vector=excluded.vector""",
            (mem.id, mem.type.value, mem.text, mem.created, mem.updated, mem.source,
             mem.confidence, mem.valid_from, mem.valid_to,
             json.dumps(mem.tags), json.dumps(mem.entities), mem.duplicate_of,
             mem.recall_count, mem.last_recalled, mem.writer, mem.ring,
             mem.due, mem.review_by,
             mem.hash, mem.meta_hash, embed_model, dim, _vec_to_blob(vector)),
        )
        self._refresh_fts_and_entities(mem)
        self.db.commit()
        self._matrix = None  # invalidate cache

    def update_meta(self, mem: Memory) -> None:
        """Refresh a row's metadata without touching its vector. This is the
        cheap path used after reflect(), a metadata-only memory_update, or a
        counter flush: the text is unchanged, so re-embedding would be wasted
        work — but confidence, links, duplicate_of, validity, counters, tags
        and entities must not go stale."""
        with self._lock:
            self.db.execute(
                """UPDATE memories SET
                     type=?, updated=?, source=?, confidence=?, valid_from=?,
                     valid_to=?, tags=?, entities=?, duplicate_of=?,
                     recall_count=?, last_recalled=?, writer=?, ring=?,
                     due=?, review_by=?, meta_hash=?
                   WHERE id=?""",
                (mem.type.value, mem.updated, mem.source, mem.confidence,
                 mem.valid_from, mem.valid_to, json.dumps(mem.tags),
                 json.dumps(mem.entities), mem.duplicate_of,
                 mem.recall_count, mem.last_recalled, mem.writer, mem.ring,
                 mem.due, mem.review_by, mem.meta_hash, mem.id),
            )
            self._refresh_fts_and_entities(mem)
            self.db.commit()

    def bump_access(self, mem_ids: Sequence[str], ts: str) -> None:
        """Record that these memories were just returned by recall. Index-side
        only and cheap; the engine batches the write-back to the canonical files."""
        if not mem_ids:
            return
        with self._lock:
            self.db.executemany(
                "UPDATE memories SET recall_count = COALESCE(recall_count,0) + 1, "
                "last_recalled = ? WHERE id = ?",
                [(ts, mid) for mid in mem_ids],
            )
            self.db.commit()

    def _refresh_fts_and_entities(self, mem: Memory) -> None:
        self.db.execute("DELETE FROM memories_fts WHERE id=?", (mem.id,))
        self.db.execute("INSERT INTO memories_fts (id, text) VALUES (?,?)", (mem.id, mem.text))
        self.db.execute("DELETE FROM mem_entities WHERE memory_id=?", (mem.id,))
        for ent in {e.strip().lower() for e in mem.entities if e.strip()}:
            self.db.execute(
                "INSERT OR IGNORE INTO mem_entities (memory_id, entity) VALUES (?,?)",
                (mem.id, ent),
            )
        self.db.execute("DELETE FROM relations WHERE src=?", (mem.id,))
        for r in mem.relations:
            self.db.execute(
                "INSERT OR REPLACE INTO relations (src, rel, dst, note) VALUES (?,?,?,?)",
                (mem.id, r["rel"], r["id"], r.get("note")))

    def delete(self, mem_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
            self.db.execute("DELETE FROM memories_fts WHERE id=?", (mem_id,))
            self.db.execute("DELETE FROM mem_entities WHERE memory_id=?", (mem_id,))
            self.db.execute("DELETE FROM relations WHERE src=?", (mem_id,))
            self.db.commit()
            self._matrix = None

    def existing_state(self) -> Dict[str, Tuple[str, str, str]]:
        """id -> (content_hash, embed_model, meta_hash), for incremental sync."""
        with self._lock:
            rows = self.db.execute(
                "SELECT id, content_hash, embed_model, meta_hash FROM memories").fetchall()
        return {r["id"]: (r["content_hash"], r["embed_model"], r["meta_hash"]) for r in rows}

    # ---- vector cache ----------------------------------------------------
    def _load_matrix_locked(self) -> None:
        rows = self.db.execute("SELECT id, dim, vector FROM memories ORDER BY id").fetchall()
        self._ids = [r["id"] for r in rows]
        if not rows:
            self._matrix = np.zeros((0, 0), dtype=np.float32)
            self._matrix_dim = None
            return
        dim = rows[0]["dim"]
        mat = np.zeros((len(rows), dim), dtype=np.float32)
        for i, r in enumerate(rows):
            if r["dim"] == dim and r["vector"] is not None:
                mat[i] = _blob_to_vec(r["vector"], dim)
        self._matrix = mat
        self._matrix_dim = dim

    def _ensure_matrix(self) -> None:
        with self._lock:
            if self._matrix is None:
                self._load_matrix_locked()

    # ---- read path: strategies ------------------------------------------
    def semantic(self, query_vec: np.ndarray, k: int,
                 types: Optional[Sequence[str]] = None,
                 rings: Optional[Sequence[str]] = None) -> List[Tuple[str, float]]:
        self._ensure_matrix()
        if self._matrix is None or self._matrix.shape[0] == 0:
            return []
        if self._matrix_dim != query_vec.shape[0]:
            # model/dim mismatch (e.g. mid-migration) -> skip semantic gracefully
            return []
        sims = self._matrix @ query_vec.astype(np.float32)   # cosine (all normalized)
        order = np.argsort(-sims)
        allow = set(types) if types else None
        allow_rings = set(rings) if rings is not None else None
        row_of: Dict[str, Tuple[str, str]] = {}
        if allow is not None or allow_rings is not None:
            with self._lock:
                rows = self.db.execute("SELECT id, type, ring FROM memories").fetchall()
            row_of = {r["id"]: (r["type"], _ring(r["ring"])) for r in rows}
        results: List[Tuple[str, float]] = []
        for idx in order:
            mid = self._ids[idx]
            if allow is not None and row_of.get(mid, ("", ""))[0] not in allow:
                continue
            if allow_rings is not None and row_of.get(mid, ("", ""))[1] not in allow_rings:
                continue
            results.append((mid, float(sims[idx])))
            if len(results) >= k:
                break
        return results

    def keyword(self, query: str, k: int,
                rings: Optional[Sequence[str]] = None) -> List[Tuple[str, float]]:
        match = _fts_query(query)
        if not match:
            return []
        ring_sql, ring_args = _ring_filter("m", rings)
        try:
            with self._lock:
                rows = self.db.execute(
                    "SELECT f.id AS id, bm25(memories_fts) AS score FROM memories_fts f "
                    "JOIN memories m ON m.id = f.id "
                    f"WHERE memories_fts MATCH ? {ring_sql} ORDER BY score LIMIT ?",
                    (match, *ring_args, k),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        # bm25 returns lower=better; convert to positive relevance
        return [(r["id"], -float(r["score"])) for r in rows]

    def graph(self, entities: Sequence[str], k: int,
              rings: Optional[Sequence[str]] = None) -> List[Tuple[str, float]]:
        ents = [e.strip().lower() for e in entities if e.strip()]
        if not ents:
            return []
        placeholders = ",".join("?" * len(ents))
        ring_sql, ring_args = _ring_filter("m", rings)
        with self._lock:
            rows = self.db.execute(
                f"""SELECT e.memory_id AS memory_id, COUNT(*) AS hits FROM mem_entities e
                    JOIN memories m ON m.id = e.memory_id
                    WHERE e.entity IN ({placeholders}) {ring_sql}
                    GROUP BY e.memory_id ORDER BY hits DESC LIMIT ?""",
                (*ents, *ring_args, k),
            ).fetchall()
        return [(r["memory_id"], float(r["hits"])) for r in rows]

    def recent(self, k: int, types: Optional[Sequence[str]] = None,
               rings: Optional[Sequence[str]] = None) -> List[str]:
        ring_sql, ring_args = _ring_filter("m", rings)
        type_sql, type_args = "", ()
        if types:
            type_sql = f"AND m.type IN ({','.join('?' * len(types))})"
            type_args = tuple(types)
        with self._lock:
            rows = self.db.execute(
                f"SELECT m.id AS id FROM memories m WHERE 1=1 {type_sql} {ring_sql} "
                "ORDER BY m.created DESC LIMIT ?", (*type_args, *ring_args, k)).fetchall()
        return [r["id"] for r in rows]

    def top_for_priming(self, k: int,
                        rings: Optional[Sequence[str]] = None) -> List[str]:
        """Best memories to inject at session start: not superseded, ranked by a
        blend of confidence, how often they're actually recalled, and recency.
        (Expired world_facts are filtered by the caller, which has the Memory.)"""
        ring_sql, ring_args = _ring_filter("m", rings)
        with self._lock:
            rows = self.db.execute(
                f"""SELECT m.id AS id FROM memories m
                   WHERE m.duplicate_of IS NULL {ring_sql}
                   ORDER BY (COALESCE(m.confidence, 0.7)
                             + 0.03 * MIN(COALESCE(m.recall_count, 0), 10)) DESC,
                            m.updated DESC
                   LIMIT ?""",
                (*ring_args, k),
            ).fetchall()
        return [r["id"] for r in rows]

    def ids_in_ring(self, ring: str) -> List[Tuple[str, str]]:
        """(id, created) for every memory in a ring, oldest first."""
        with self._lock:
            rows = self.db.execute(
                "SELECT id, created FROM memories WHERE COALESCE(ring, 'canon') = ? "
                "ORDER BY created, id", (ring,)).fetchall()
        return [(r["id"], r["created"]) for r in rows]

    # ---- relations & business types ----------------------------------------
    def relation_edges(self, rel: Optional[str] = None) -> List[Tuple[str, str, str, str]]:
        """(src, rel, dst, src_ring) for every edge whose source is a live
        memory. Edges from proposals are excluded: a relation takes effect only
        once the memory carrying it has been reviewed into a real ring."""
        rel_sql, args = ("AND r.rel = ?", (rel,)) if rel else ("", ())
        with self._lock:
            rows = self.db.execute(
                f"""SELECT r.src AS src, r.rel AS rel, r.dst AS dst,
                           COALESCE(m.ring, 'canon') AS ring
                    FROM relations r JOIN memories m ON m.id = r.src
                    WHERE COALESCE(m.ring, 'canon') != 'proposed'
                      AND m.duplicate_of IS NULL {rel_sql}""", args).fetchall()
        return [(r["src"], r["rel"], r["dst"], r["ring"]) for r in rows]

    def ids_by_type_dated(self, type_: str, column: str, until: str,
                          rings: Optional[Sequence[str]] = None) -> List[Tuple[str, str]]:
        """(id, date) for memories of `type_` whose `column` (due/review_by)
        is set and <= `until`, soonest first. Superseded rows are skipped."""
        assert column in ("due", "review_by")
        ring_sql, ring_args = _ring_filter("m", rings)
        with self._lock:
            rows = self.db.execute(
                f"""SELECT m.id AS id, m.{column} AS d FROM memories m
                    WHERE m.type = ? AND m.{column} IS NOT NULL AND m.{column} != ''
                      AND substr(m.{column}, 1, 10) <= ? AND m.duplicate_of IS NULL
                      {ring_sql}
                    ORDER BY m.{column}, m.id""",
                (type_, until, *ring_args)).fetchall()
        return [(r["id"], r["d"]) for r in rows]

    def known_rings(self) -> List[str]:
        with self._lock:
            rows = self.db.execute(
                "SELECT DISTINCT COALESCE(ring, 'canon') AS ring FROM memories").fetchall()
        return [r["ring"] for r in rows]

    # ---- helpers ---------------------------------------------------------
    def get_meta(self, mem_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT id,type,text,created,updated,source,confidence,valid_from,"
                "valid_to,tags,entities,duplicate_of,recall_count,last_recalled,"
                "writer,ring,embed_model FROM memories WHERE id=?",
                (mem_id,)).fetchone()

    def all_vectors(self) -> Tuple[List[str], np.ndarray]:
        self._ensure_matrix()
        return list(self._ids), (self._matrix if self._matrix is not None
                                 else np.zeros((0, 0), np.float32))

    def stats(self) -> Dict[str, object]:
        with self._lock:
            total = self.db.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
            by_type = {r["type"]: r["c"] for r in self.db.execute(
                "SELECT type, COUNT(*) c FROM memories GROUP BY type").fetchall()}
            models = {r["embed_model"]: r["c"] for r in self.db.execute(
                "SELECT embed_model, COUNT(*) c FROM memories GROUP BY embed_model").fetchall()}
            superseded = self.db.execute(
                "SELECT COUNT(*) c FROM memories WHERE duplicate_of IS NOT NULL").fetchone()["c"]
            never_recalled = self.db.execute(
                "SELECT COUNT(*) c FROM memories "
                "WHERE COALESCE(recall_count, 0) = 0").fetchone()["c"]
            by_ring = {r["ring"]: r["c"] for r in self.db.execute(
                "SELECT COALESCE(ring, 'canon') ring, COUNT(*) c FROM memories "
                "GROUP BY 1").fetchall()}
        return {"total": total, "by_type": by_type, "by_ring": by_ring,
                "by_embed_model": models,
                "superseded": superseded, "never_recalled": never_recalled}


def _ring(value: Optional[str]) -> str:
    return value or "canon"


def _ring_filter(alias: str, rings: Optional[Sequence[str]]) -> Tuple[str, tuple]:
    """SQL fragment restricting `alias` to the given rings (None = no filter).
    A missing ring column value reads as canon, like a ring-less note."""
    if rings is None:
        return "", ()
    rings = list(rings)
    if not rings:
        return "AND 0", ()
    return (f"AND COALESCE({alias}.ring, 'canon') IN ({','.join('?' * len(rings))})",
            tuple(rings))


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query over its word tokens."""
    toks = [w for w in "".join(c if c.isalnum() else " " for c in query).split() if len(w) > 1]
    return " OR ".join(toks)


# ---- full (re)build from the canonical store ----------------------------
def reindex(store, index: "MemoryIndex", provider: EmbeddingProvider,
            force: bool = False) -> Dict[str, int]:
    """Bring the index in line with the notes on disk. Three-way incremental:

      * text or embedding model changed (or force) -> re-embed + full upsert
      * only metadata changed (reflect edits, counter flushes, hand edits to
        frontmatter, synced changes from another device) -> cheap metadata refresh
      * nothing changed                            -> skip

    Returns counts of what happened."""
    existing = index.existing_state()
    seen: set = set()
    to_embed: List[Memory] = []
    meta_updated = 0
    unchanged = 0

    for mem in store.iter_memories():
        seen.add(mem.id)
        prev = existing.get(mem.id)
        needs_embed = (force or prev is None
                       or prev[0] != mem.hash          # text changed
                       or prev[1] != provider.name)     # model changed (evolutionary)
        if needs_embed:
            to_embed.append(mem)
        elif prev[2] != mem.meta_hash:                   # frontmatter-only change
            index.update_meta(mem)
            meta_updated += 1
        else:
            unchanged += 1

    # batch-embed for efficiency (the provider chunks + retries internally)
    if to_embed:
        vecs = provider.embed([m.text for m in to_embed])
        for mem, vec in zip(to_embed, vecs):
            index.upsert(mem, vec, provider.name)

    # drop index rows whose note was deleted on disk
    removed = 0
    for stale_id in set(existing) - seen:
        index.delete(stale_id)
        removed += 1

    return {"embedded": len(to_embed), "meta_updated": meta_updated,
            "unchanged": unchanged, "removed": removed}
