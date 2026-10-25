"""Postgres backend: the canonical store, the journal and the derived index in
one Postgres database (Supabase works), one *workspace* (tenant) per store.

Why: the file backend is perfect for one machine and a handful of agents, but
a large team of agents needs concurrent writers, millions of memories, and a
store every service (Vercel functions included) can reach. Postgres gives
transactions, pgvector (HNSW) for semantic search, full-text search, managed
backups and point-in-time recovery.

The architecture is unchanged — the same three roles, behind the same
interfaces the engine already uses:

  PgStore    canonical memories        mnemosyne.memories   (doc = full state)
  PgJournal  hash-chained event log    mnemosyne.journal    (same events, same hashes)
  PgIndex    derived, rebuildable      mnemosyne.idx (+ idx_entities,
                                       idx_relations, idx_reputation)

The canonical/derived split still holds: `mnemosyne reindex --force` rebuilds
every `idx*` row from `memories`. And the owner can still walk away with
everything: `mnemosyne export --out DIR` writes the exact file-backend layout
(Markdown notes + journal shards) — the exported journal verifies with the
same hash chain, and the export is a working file-backend store.

Concurrency: within a process the engine's lock serializes operations; across
processes (several server replicas) every engine operation holds a
per-workspace Postgres advisory lock, so read-modify-write sequences and the
journal chain stay linear per workspace while different workspaces run fully
in parallel. Use a *session* connection (Supabase: direct connection or the
session pooler, port 5432) — transaction pooling breaks advisory locks.

Requires: psycopg 3 (`pip install mnemosyne-memory[postgres]`) and the
`vector` extension (pgvector >= 0.5; >= 0.8 recommended for filtered HNSW).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import numpy as np

from .journal import _bad, event_hash, GENESIS, memory_from_state
from .schema import Memory, content_hash, slugify, utcnow_iso

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised only without the extra
    psycopg = None

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _vec_text(v: np.ndarray) -> str:
    return "[" + ",".join(f"{float(x):.7g}" for x in np.asarray(v, dtype=np.float32)) + "]"


def _vec_parse(s: str) -> np.ndarray:
    return np.array([float(x) for x in s.strip("[]").split(",") if x], dtype=np.float32)


class PgDB:
    """One connection per engine, guarded by a lock (like the SQLite index).
    Also provides the per-workspace advisory lock used by the engine."""

    def __init__(self, url: str, workspace: str, schema: str = "mnemosyne"):
        if psycopg is None:
            raise RuntimeError("the postgres backend needs psycopg 3: "
                               "pip install 'mnemosyne-memory[postgres]'")
        if not url:
            raise RuntimeError("storage = postgres needs MNEMOSYNE_DATABASE_URL "
                               "(a session connection string; never put it in a file)")
        if not _SCHEMA_RE.match(schema):
            raise ValueError(f"invalid postgres schema name {schema!r}")
        self.url, self.workspace, self.schema = url, workspace, schema
        self._lock = threading.RLock()
        self.conn = self._connect()
        self._lock_key = int.from_bytes(
            hashlib.sha256(f"mnemosyne:{schema}:{workspace}".encode()).digest()[:8],
            "big", signed=True)
        self.ensure_schema()

    def _connect(self):
        return psycopg.connect(self.url, autocommit=True, row_factory=dict_row,
                               prepare_threshold=None)

    def q(self, sql: str, args: Sequence = (), fetch: str = "all"):
        """Run one statement. `{s}` in the SQL is the schema name."""
        sql = sql.replace("{s}", self.schema)
        with self._lock:
            cur = self.conn.execute(sql, args)
            if fetch == "all":
                return cur.fetchall() if cur.description else []
            if fetch == "one":
                return cur.fetchone() if cur.description else None
            return None

    def transaction(self):
        return self.conn.transaction()

    # ---- cross-process write serialization ----------------------------------
    def acquire(self) -> None:
        self.q("SELECT pg_advisory_lock(%s)", (self._lock_key,), fetch="none")

    def release(self) -> None:
        if self.conn.closed:
            return  # closing the session already released its advisory locks
        self.q("SELECT pg_advisory_unlock(%s)", (self._lock_key,), fetch="none")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---- schema ---------------------------------------------------------------
    def ensure_schema(self) -> None:
        with self._lock, self.conn.transaction():
            # one-time DDL guarded by a global advisory lock (replicas racing)
            self.conn.execute("SELECT pg_advisory_xact_lock(7314)")
            self.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            for stmt in _DDL.replace("{s}", self.schema).split(";\n"):
                if stmt.strip():
                    self.conn.execute(stmt)

    def ensure_vector_index(self, dim: int) -> None:
        """HNSW index for one embedding dimension (partial on dim, so stores
        mid-migration between models of different sizes keep working)."""
        name = f"idx_emb_hnsw_{int(dim)}"
        self.q(f"CREATE INDEX IF NOT EXISTS {name} ON {{s}}.idx USING hnsw "
               f"((embedding::vector({int(dim)})) vector_cosine_ops) WHERE dim = {int(dim)}",
               fetch="none")


_DDL = """
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.memories (
    workspace   text NOT NULL,
    id          text NOT NULL,
    ring        text NOT NULL,
    doc         jsonb NOT NULL,
    written_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, id)
);
CREATE TABLE IF NOT EXISTS {s}.archive (
    workspace   text NOT NULL,
    ref         text NOT NULL,
    content     text NOT NULL,
    PRIMARY KEY (workspace, ref)
);
CREATE TABLE IF NOT EXISTS {s}.journal (
    workspace   text NOT NULL,
    seq         bigint NOT NULL,
    ts          text NOT NULL,
    hash        text NOT NULL,
    event       jsonb NOT NULL,
    PRIMARY KEY (workspace, seq)
);
CREATE TABLE IF NOT EXISTS {s}.idx (
    workspace     text NOT NULL,
    id            text NOT NULL,
    type          text NOT NULL,
    text          text NOT NULL,
    created       text NOT NULL,
    updated       text NOT NULL,
    source        text,
    confidence    real,
    valid_from    text,
    valid_to      text,
    tags          jsonb NOT NULL DEFAULT '[]',
    entities      jsonb NOT NULL DEFAULT '[]',
    duplicate_of  text,
    recall_count  integer NOT NULL DEFAULT 0,
    last_recalled text,
    writer        text,
    ring          text,
    due           text,
    review_by     text,
    content_hash  text,
    meta_hash     text,
    embed_model   text,
    dim           integer,
    embedding     vector,
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    PRIMARY KEY (workspace, id)
);
CREATE INDEX IF NOT EXISTS idx_ws_ring ON {s}.idx (workspace, ring);
CREATE INDEX IF NOT EXISTS idx_ws_type ON {s}.idx (workspace, type);
CREATE INDEX IF NOT EXISTS idx_ws_created ON {s}.idx (workspace, created);
CREATE INDEX IF NOT EXISTS idx_tsv ON {s}.idx USING gin (tsv);
CREATE TABLE IF NOT EXISTS {s}.idx_entities (
    workspace text NOT NULL, memory_id text NOT NULL, entity text NOT NULL,
    PRIMARY KEY (workspace, memory_id, entity)
);
CREATE INDEX IF NOT EXISTS idx_ent_lookup ON {s}.idx_entities (workspace, entity);
CREATE TABLE IF NOT EXISTS {s}.idx_relations (
    workspace text NOT NULL, src text NOT NULL, rel text NOT NULL, dst text NOT NULL,
    note text, PRIMARY KEY (workspace, src, rel, dst)
);
CREATE TABLE IF NOT EXISTS {s}.idx_reputation (
    workspace text NOT NULL, writer text NOT NULL, resolved integer NOT NULL,
    brier real NOT NULL, reputation real NOT NULL, multiplier real NOT NULL,
    PRIMARY KEY (workspace, writer)
)
"""


def _ring_sql(rings: Optional[Sequence[str]], col: str = "ring") -> Tuple[str, list]:
    if rings is None:
        return "", []
    rings = list(rings)
    if not rings:
        return "AND false", []
    return f"AND COALESCE({col}, 'canon') = ANY(%s)", [rings]


def _id_sql(ids: Optional[Set[str]], col: str = "id") -> Tuple[str, list]:
    if ids is None:
        return "", []
    if not ids:
        return "AND false", []
    return f"AND {col} = ANY(%s)", [sorted(ids)]


# --------------------------------------------------------------------------- #
# canonical store
# --------------------------------------------------------------------------- #
class PgStore:
    """Canonical memories as rows. Same interface as MarkdownStore."""

    supports_files = False

    def __init__(self, db: PgDB, root: Path):
        self.db, self.ws, self.root = db, db.workspace, Path(root)
        self.notes_dir = self.root / "notes"        # only used by exports
        self.archive_dir = self.root / "archive"

    def write(self, mem: Memory) -> None:
        self.db.q("INSERT INTO {s}.memories (workspace, id, ring, doc) VALUES (%s,%s,%s,%s) "
                  "ON CONFLICT (workspace, id) DO UPDATE SET ring = excluded.ring, "
                  "doc = excluded.doc, written_at = now()",
                  (self.ws, mem.id, mem.ring, json.dumps(mem.to_json())), fetch="none")

    def read_by_id(self, mem_id: str) -> Optional[Memory]:
        row = self.db.q("SELECT doc FROM {s}.memories WHERE workspace=%s AND id=%s",
                        (self.ws, mem_id), fetch="one")
        return memory_from_state(row["doc"]) if row else None

    def read_many(self, ids: Sequence[str]) -> Dict[str, Memory]:
        if not ids:
            return {}
        rows = self.db.q("SELECT id, doc FROM {s}.memories WHERE workspace=%s AND id = ANY(%s)",
                         (self.ws, list(ids)))
        return {r["id"]: memory_from_state(r["doc"]) for r in rows}

    def delete(self, mem_id: str) -> bool:
        rows = self.db.q("DELETE FROM {s}.memories WHERE workspace=%s AND id=%s RETURNING id",
                         (self.ws, mem_id))
        return bool(rows)

    def iter_memories(self) -> Iterator[Memory]:
        for r in self.db.q("SELECT doc FROM {s}.memories WHERE workspace=%s ORDER BY id",
                           (self.ws,)):
            yield memory_from_state(r["doc"])

    def count(self) -> int:
        return self.db.q("SELECT count(*) AS n FROM {s}.memories WHERE workspace=%s",
                         (self.ws,), fetch="one")["n"]

    def path_by_id(self, mem_id: str) -> None:
        return None  # no files: memories are rows (export to get Markdown)

    def archive_raw(self, text: str, label: str = "session") -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        ref = f"archive/{ts}__{slugify(label)}__{content_hash(text)}.md"
        body = f"<!-- archived {utcnow_iso()} | label={label} -->\n\n{text.strip()}\n"
        self.db.q("INSERT INTO {s}.archive (workspace, ref, content) VALUES (%s,%s,%s) "
                  "ON CONFLICT DO NOTHING", (self.ws, ref, body), fetch="none")
        return self.root / ref

    def read_archive(self, ref: str) -> Optional[str]:
        row = self.db.q("SELECT content FROM {s}.archive WHERE workspace=%s AND ref=%s",
                        (self.ws, ref), fetch="one")
        return row["content"] if row else None

    def archive_items(self) -> Iterator[Tuple[str, str]]:
        for r in self.db.q("SELECT ref, content FROM {s}.archive WHERE workspace=%s ORDER BY ref",
                           (self.ws,)):
            yield r["ref"], r["content"]


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #
class PgJournal:
    """The hash-chained journal as rows: identical events and hashes to the
    file journal, so an export verifies with the same chain."""

    def __init__(self, db: PgDB):
        self.db, self.ws = db, db.workspace

    def append(self, op: str, writer: str, memory_id: Optional[str] = None,
               mem: Optional[Memory] = None, **extra: Any) -> Dict[str, Any]:
        from . import journal as file_journal  # the clock tests patch lives there
        event: Dict[str, Any] = {"ts": file_journal.utcnow_iso(), "writer": writer, "op": op,
                                 "memory_id": memory_id or (mem.id if mem else None)}
        if mem is not None:
            event["content_hash"] = mem.hash
            event["meta_hash"] = mem.meta_hash
            event["state"] = mem.to_json()
        event.update({k: v for k, v in extra.items() if v is not None})
        with self.db._lock, self.db.transaction():
            c = self.db.conn
            c.execute("SELECT pg_advisory_xact_lock(%s)", (self.db._lock_key ^ 1,))
            head = c.execute(f"SELECT seq, hash FROM {self.db.schema}.journal "
                             "WHERE workspace=%s ORDER BY seq DESC LIMIT 1",
                             (self.ws,)).fetchone()
            event["prev_hash"] = head["hash"] if head else GENESIS
            event["hash"] = event_hash(event)
            c.execute(f"INSERT INTO {self.db.schema}.journal (workspace, seq, ts, hash, event) "
                      "VALUES (%s,%s,%s,%s,%s)",
                      (self.ws, (head["seq"] + 1) if head else 1, event["ts"], event["hash"],
                       json.dumps(event, ensure_ascii=False, sort_keys=True)))
        return event

    def events(self, until: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        rows = self.db.q("SELECT event FROM {s}.journal WHERE workspace=%s ORDER BY seq",
                         (self.ws,))
        for r in rows:
            ev = r["event"]
            if until is not None and ev["ts"] > until:
                return
            yield ev

    def verify(self) -> Dict[str, Any]:
        expected, count = GENESIS, 0
        for r in self.db.q("SELECT seq, event FROM {s}.journal WHERE workspace=%s ORDER BY seq",
                           (self.ws,)):
            ev, where = r["event"], f"journal seq {r['seq']}"
            if ev.get("prev_hash") != expected:
                return _bad(count, 0, where, "prev_hash does not match the previous event "
                                             "(a row was removed, reordered, or inserted)")
            if ev.get("hash") != event_hash(ev):
                return _bad(count, 0, where, "hash does not match the event's content "
                                             "(it was edited)")
            expected = ev["hash"]
            count += 1
        return {"ok": True, "events": count, "legacy_events": 0, "head": expected,
                "error": None}

    def export_shards(self, out_dir: Path) -> List[Path]:
        """Write the journal in the file backend's layout (monthly shards)."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        shards: Dict[str, List[str]] = {}
        for ev in self.events():
            shards.setdefault(ev["ts"][:7], []).append(
                json.dumps(ev, ensure_ascii=False, sort_keys=True))
        paths = []
        for month, lines in sorted(shards.items()):
            p = out_dir / f"events-{month}.jsonl"
            p.write_text("\n".join(lines) + "\n", encoding="utf-8")
            paths.append(p)
        return paths


# --------------------------------------------------------------------------- #
# derived index
# --------------------------------------------------------------------------- #
_COLS = ("id,type,text,created,updated,source,confidence,valid_from,valid_to,tags,"
         "entities,duplicate_of,recall_count,last_recalled,writer,ring,due,review_by")


class PgIndex:
    """Same interface as index.MemoryIndex, on Postgres + pgvector + FTS."""

    def __init__(self, db: PgDB, exact_search_max: int = 20000):
        self.db, self.ws = db, db.workspace
        self.exact_search_max = exact_search_max
        self._dims: Set[int] = set()

    def close(self) -> None:
        self.db.close()

    # ---- write path ---------------------------------------------------------
    def upsert(self, mem: Memory, vector: np.ndarray, embed_model: str) -> None:
        dim = int(vector.shape[0])
        if dim not in self._dims:
            self.db.ensure_vector_index(dim)
            self._dims.add(dim)
        with self.db._lock, self.db.transaction():
            self.db.conn.execute(
                f"""INSERT INTO {self.db.schema}.idx
                   (workspace,{_COLS},content_hash,meta_hash,embed_model,dim,embedding)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s::vector)
                   ON CONFLICT (workspace,id) DO UPDATE SET
                     type=excluded.type, text=excluded.text, created=excluded.created,
                     updated=excluded.updated, source=excluded.source,
                     confidence=excluded.confidence, valid_from=excluded.valid_from,
                     valid_to=excluded.valid_to, tags=excluded.tags,
                     entities=excluded.entities, duplicate_of=excluded.duplicate_of,
                     recall_count=excluded.recall_count,
                     last_recalled=excluded.last_recalled, writer=excluded.writer,
                     ring=excluded.ring, due=excluded.due, review_by=excluded.review_by,
                     content_hash=excluded.content_hash, meta_hash=excluded.meta_hash,
                     embed_model=excluded.embed_model, dim=excluded.dim,
                     embedding=excluded.embedding""",
                (self.ws, *self._meta_args(mem), mem.hash, mem.meta_hash, embed_model, dim,
                 _vec_text(vector)))
            self._refresh_side_tables(mem)

    @staticmethod
    def _meta_args(mem: Memory) -> tuple:
        return (mem.id, mem.type.value, mem.text, mem.created, mem.updated, mem.source,
                mem.confidence, mem.valid_from, mem.valid_to, json.dumps(mem.tags),
                json.dumps(mem.entities), mem.duplicate_of, mem.recall_count,
                mem.last_recalled, mem.writer, mem.ring, mem.due, mem.review_by)

    def update_meta(self, mem: Memory) -> None:
        with self.db._lock, self.db.transaction():
            self.db.conn.execute(
                f"""UPDATE {self.db.schema}.idx SET type=%s, updated=%s, source=%s,
                   confidence=%s, valid_from=%s, valid_to=%s, tags=%s, entities=%s,
                   duplicate_of=%s, recall_count=%s, last_recalled=%s, writer=%s, ring=%s,
                   due=%s, review_by=%s, meta_hash=%s WHERE workspace=%s AND id=%s""",
                (mem.type.value, mem.updated, mem.source, mem.confidence, mem.valid_from,
                 mem.valid_to, json.dumps(mem.tags), json.dumps(mem.entities),
                 mem.duplicate_of, mem.recall_count, mem.last_recalled, mem.writer, mem.ring,
                 mem.due, mem.review_by, mem.meta_hash, self.ws, mem.id))
            self._refresh_side_tables(mem)

    def _refresh_side_tables(self, mem: Memory) -> None:
        c, s = self.db.conn, self.db.schema
        c.execute(f"DELETE FROM {s}.idx_entities WHERE workspace=%s AND memory_id=%s",
                  (self.ws, mem.id))
        ents = sorted({e.strip().lower() for e in mem.entities if e.strip()})
        if ents:
            c.cursor().executemany(
                f"INSERT INTO {s}.idx_entities (workspace, memory_id, entity) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING", [(self.ws, mem.id, e) for e in ents])
        c.execute(f"DELETE FROM {s}.idx_relations WHERE workspace=%s AND src=%s",
                  (self.ws, mem.id))
        if mem.relations:
            c.cursor().executemany(
                f"INSERT INTO {s}.idx_relations (workspace, src, rel, dst, note) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (workspace, src, rel, dst) "
                "DO UPDATE SET note = excluded.note",
                [(self.ws, mem.id, r["rel"], r["id"], r.get("note")) for r in mem.relations])

    def bump_access(self, mem_ids: Sequence[str], ts: str) -> None:
        if mem_ids:
            self.db.q("UPDATE {s}.idx SET recall_count = recall_count + 1, last_recalled = %s "
                      "WHERE workspace=%s AND id = ANY(%s)", (ts, self.ws, list(mem_ids)),
                      fetch="none")

    def delete(self, mem_id: str) -> None:
        with self.db._lock, self.db.transaction():
            for tbl, col in (("idx", "id"), ("idx_entities", "memory_id"),
                             ("idx_relations", "src")):
                self.db.conn.execute(
                    f"DELETE FROM {self.db.schema}.{tbl} WHERE workspace=%s AND {col}=%s",
                    (self.ws, mem_id))

    def existing_state(self) -> Dict[str, Tuple[str, str, str]]:
        rows = self.db.q("SELECT id, content_hash, embed_model, meta_hash FROM {s}.idx "
                         "WHERE workspace=%s", (self.ws,))
        return {r["id"]: (r["content_hash"], r["embed_model"], r["meta_hash"]) for r in rows}

    # ---- read path: strategies ---------------------------------------------
    def semantic(self, query_vec: np.ndarray, k: int,
                 types: Optional[Sequence[str]] = None,
                 rings: Optional[Sequence[str]] = None,
                 only: Optional[Set[str]] = None) -> List[Tuple[str, float]]:
        dim = int(query_vec.shape[0])
        ring_sql, ring_args = _ring_sql(rings)
        id_sql, id_args = _id_sql(only)
        type_sql, type_args = ("AND type = ANY(%s)", [list(types)]) if types else ("", [])
        where = f"workspace=%s AND dim=%s {ring_sql} {id_sql} {type_sql}"
        args = [self.ws, dim, *ring_args, *id_args, *type_args]
        q = _vec_text(query_vec)
        n = self.db.q(f"SELECT count(*) AS n FROM {{s}}.idx WHERE {where}", args,
                      fetch="one")["n"]
        if n == 0:
            return []
        if n <= self.exact_search_max:
            # exact cosine over this workspace's rows: perfect recall, and
            # fast at this size (the HNSW index is not used: its expression
            # is the dim-cast, this one is not)
            rows = self.db.q(
                f"SELECT id, 1 - (embedding <=> %s::vector) AS sim FROM {{s}}.idx "
                f"WHERE {where} ORDER BY embedding <=> %s::vector, id LIMIT %s",
                [q, *args, q, k])
        else:
            with self.db._lock, self.db.transaction():
                c = self.db.conn
                c.execute(f"SET LOCAL hnsw.ef_search = {max(100, k * 4)}")
                try:  # pgvector >= 0.8: keep scanning until filters are satisfied
                    with c.transaction():
                        c.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                except Exception:
                    pass
                rows = c.execute(
                    f"SELECT id, 1 - ((embedding::vector({dim})) <=> %s::vector({dim})) AS sim "
                    f"FROM {self.db.schema}.idx WHERE {where} "
                    f"ORDER BY (embedding::vector({dim})) <=> %s::vector({dim}) LIMIT %s",
                    [q, *args, q, k]).fetchall()
            rows = sorted(rows, key=lambda r: (-r["sim"], r["id"]))
        return [(r["id"], float(r["sim"])) for r in rows]

    def keyword(self, query: str, k: int, rings: Optional[Sequence[str]] = None,
                only: Optional[Set[str]] = None) -> List[Tuple[str, float]]:
        toks = [w.lower() for w in "".join(c if c.isalnum() else " " for c in query).split()
                if len(w) > 1]
        if not toks:
            return []
        ring_sql, ring_args = _ring_sql(rings)
        id_sql, id_args = _id_sql(only)
        rows = self.db.q(
            # normalization 2 = divide by document length: like SQLite's BM25,
            # a short exact entry beats a long one with incidental matches
            f"""SELECT id, ts_rank_cd(tsv, q, 2) AS score
                FROM {{s}}.idx, to_tsquery('english', %s) q
                WHERE workspace=%s AND tsv @@ q {ring_sql} {id_sql}
                ORDER BY score DESC, id LIMIT %s""",
            [" | ".join(toks), self.ws, *ring_args, *id_args, k])
        return [(r["id"], float(r["score"])) for r in rows]

    def graph(self, entities: Sequence[str], k: int,
              rings: Optional[Sequence[str]] = None,
              only: Optional[Set[str]] = None) -> List[Tuple[str, float]]:
        ents = [e.strip().lower() for e in entities if e.strip()]
        if not ents:
            return []
        ring_sql, ring_args = _ring_sql(rings, "m.ring")
        id_sql, id_args = _id_sql(only, "m.id")
        rows = self.db.q(
            f"""SELECT e.memory_id AS id, count(*) AS hits FROM {{s}}.idx_entities e
                JOIN {{s}}.idx m ON m.workspace = e.workspace AND m.id = e.memory_id
                WHERE e.workspace=%s AND e.entity = ANY(%s) {ring_sql} {id_sql}
                GROUP BY e.memory_id ORDER BY hits DESC, e.memory_id LIMIT %s""",
            [self.ws, ents, *ring_args, *id_args, k])
        return [(r["id"], float(r["hits"])) for r in rows]

    def recent(self, k: int, types: Optional[Sequence[str]] = None,
               rings: Optional[Sequence[str]] = None) -> List[str]:
        ring_sql, ring_args = _ring_sql(rings)
        type_sql, type_args = ("AND type = ANY(%s)", [list(types)]) if types else ("", [])
        rows = self.db.q(f"SELECT id FROM {{s}}.idx WHERE workspace=%s {type_sql} {ring_sql} "
                         "ORDER BY created DESC, id DESC LIMIT %s",
                         [self.ws, *type_args, *ring_args, k])
        return [r["id"] for r in rows]

    def top_for_priming(self, k: int, rings: Optional[Sequence[str]] = None) -> List[str]:
        ring_sql, ring_args = _ring_sql(rings)
        rows = self.db.q(
            f"""SELECT id FROM {{s}}.idx WHERE workspace=%s AND duplicate_of IS NULL {ring_sql}
                ORDER BY (COALESCE(confidence, 0.7) + 0.03 * LEAST(recall_count, 10)) DESC,
                         updated DESC LIMIT %s""", [self.ws, *ring_args, k])
        return [r["id"] for r in rows]

    def ids_in_ring(self, ring: str) -> List[Tuple[str, str]]:
        rows = self.db.q("SELECT id, created FROM {s}.idx WHERE workspace=%s "
                         "AND COALESCE(ring, 'canon') = %s ORDER BY created, id", (self.ws, ring))
        return [(r["id"], r["created"]) for r in rows]

    def known_rings(self) -> List[str]:
        rows = self.db.q("SELECT DISTINCT COALESCE(ring, 'canon') AS ring FROM {s}.idx "
                         "WHERE workspace=%s", (self.ws,))
        return [r["ring"] for r in rows]

    def relation_edges(self, rel: Optional[str] = None) -> List[Tuple[str, str, str, str]]:
        rel_sql, args = ("AND r.rel = %s", [rel]) if rel else ("", [])
        rows = self.db.q(
            f"""SELECT r.src, r.rel, r.dst, COALESCE(m.ring, 'canon') AS ring
                FROM {{s}}.idx_relations r
                JOIN {{s}}.idx m ON m.workspace = r.workspace AND m.id = r.src
                WHERE r.workspace=%s AND COALESCE(m.ring, 'canon') != 'proposed'
                  AND m.duplicate_of IS NULL {rel_sql}""", [self.ws, *args])
        return [(r["src"], r["rel"], r["dst"], r["ring"]) for r in rows]

    def ids_by_type_dated(self, type_: str, column: str, until: str,
                          rings: Optional[Sequence[str]] = None) -> List[Tuple[str, str]]:
        assert column in ("due", "review_by")
        ring_sql, ring_args = _ring_sql(rings)
        rows = self.db.q(
            f"""SELECT id, {column} AS d FROM {{s}}.idx
                WHERE workspace=%s AND type=%s AND {column} IS NOT NULL AND {column} != ''
                  AND substr({column}, 1, 10) <= %s AND duplicate_of IS NULL {ring_sql}
                ORDER BY {column}, id""", [self.ws, type_, until, *ring_args])
        return [(r["id"], r["d"]) for r in rows]

    def ids_with_tag_prefix(self, prefix: str, rings: Optional[Sequence[str]] = None
                            ) -> List[Tuple[str, List[str]]]:
        ring_sql, ring_args = _ring_sql(rings)
        rows = self.db.q(
            f"""SELECT id, tags FROM {{s}}.idx WHERE workspace=%s {ring_sql}
                AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(tags) t
                            WHERE left(t, %s) = %s)""",
            [self.ws, *ring_args, len(prefix), prefix])
        return [(r["id"], list(r["tags"])) for r in rows]

    def set_reputations(self, reps: Dict[str, Dict[str, float]]) -> None:
        with self.db._lock, self.db.transaction():
            c = self.db.conn
            c.execute(f"DELETE FROM {self.db.schema}.idx_reputation WHERE workspace=%s",
                      (self.ws,))
            if reps:
                c.cursor().executemany(
                    f"INSERT INTO {self.db.schema}.idx_reputation "
                    "(workspace, writer, resolved, brier, reputation, multiplier) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    [(self.ws, w, r["resolved"], r["brier"], r["reputation"], r["multiplier"])
                     for w, r in reps.items()])

    def reputations(self) -> Dict[str, Dict[str, float]]:
        rows = self.db.q("SELECT * FROM {s}.idx_reputation WHERE workspace=%s ORDER BY writer",
                         (self.ws,))
        return {r["writer"]: {"resolved": r["resolved"], "brier": round(r["brier"], 4),
                              "reputation": round(r["reputation"], 4),
                              "multiplier": round(r["multiplier"], 4)} for r in rows}

    # ---- helpers --------------------------------------------------------------
    def get_meta(self, mem_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.q(f"SELECT {_COLS}, embed_model FROM {{s}}.idx "
                        "WHERE workspace=%s AND id=%s", (self.ws, mem_id), fetch="one")
        if row is None:
            return None
        row = dict(row)
        row["tags"], row["entities"] = json.dumps(row["tags"]), json.dumps(row["entities"])
        return row

    def all_vectors(self) -> Tuple[List[str], np.ndarray]:
        """Every vector of the most common dimension (reflect's input)."""
        top = self.db.q("SELECT dim FROM {s}.idx WHERE workspace=%s GROUP BY dim "
                        "ORDER BY count(*) DESC LIMIT 1", (self.ws,), fetch="one")
        if not top:
            return [], np.zeros((0, 0), np.float32)
        rows = self.db.q("SELECT id, embedding::text AS v FROM {s}.idx "
                         "WHERE workspace=%s AND dim=%s ORDER BY id", (self.ws, top["dim"]))
        if not rows:
            return [], np.zeros((0, 0), np.float32)
        return [r["id"] for r in rows], np.vstack([_vec_parse(r["v"]) for r in rows])

    def stats(self) -> Dict[str, object]:
        ws = (self.ws,)
        one = lambda sql: self.db.q(sql, ws, fetch="one")["n"]
        grouped = lambda col: {r["k"]: r["n"] for r in self.db.q(
            f"SELECT {col} AS k, count(*) AS n FROM {{s}}.idx WHERE workspace=%s GROUP BY 1",
            ws)}
        return {"total": one("SELECT count(*) AS n FROM {s}.idx WHERE workspace=%s"),
                "by_type": grouped("type"),
                "by_ring": grouped("COALESCE(ring, 'canon')"),
                "by_embed_model": grouped("embed_model"),
                "superseded": one("SELECT count(*) AS n FROM {s}.idx WHERE workspace=%s "
                                  "AND duplicate_of IS NOT NULL"),
                "never_recalled": one("SELECT count(*) AS n FROM {s}.idx WHERE workspace=%s "
                                      "AND recall_count = 0")}


def open_backend(cfg) -> Tuple[PgDB, PgStore, PgJournal, PgIndex]:
    db = PgDB(cfg.database_url, cfg.workspace, cfg.pg_schema)
    return db, PgStore(db, cfg.root), PgJournal(db), PgIndex(db, cfg.exact_search_max)
