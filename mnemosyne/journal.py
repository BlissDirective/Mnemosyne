"""The journal: an append-only, hash-chained event log — the system of record.

Every mutation is appended here *before* the notes are written, so `notes/`
is the materialized current state of the journal (`replay` rebuilds it, byte
for byte). Events live in monthly shards next to `notes/` and are canonical
(Git-synced):

    journal/events.jsonl            legacy (v0.4-v0.8, unchained) — read first
    journal/events-2026-10.jsonl    one shard per UTC month

Each event carries

    ts, writer, op, memory_id, content_hash, meta_hash, state, prev_hash, hash

where `state` is the memory's full frontmatter + text after the change, and
`hash` = sha256 of the event's canonical JSON (everything but `hash`), so
every event commits to its predecessor. Rewriting, deleting, or reordering any
line breaks the chain at that point — `verify()` finds it in one pass. The
first chained event anchors to the sha256 of the legacy file, if any. (Cutting
events off the *end* can only be caught against a head hash recorded
elsewhere — e.g. the Git history — which `verify()` reports for that purpose.)

Appends take an OS-level file lock, so two processes on one machine cannot
fork the chain. Two *machines* both writing and merging the journal through
Git would fork it; run one writer process (the HTTP server) per store.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .schema import Memory, MemoryType, utcnow_iso

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows: in-process lock only
    fcntl = None

# ops that leave the memory present on disk (and so carry `state`)
STATE_OPS = {"remember", "update", "reflect", "access", "propose", "promote",
             "reject", "supersede", "relate", "resolve", "external"}
DELETE_OPS = {"forget", "external_delete"}
GENESIS = "0" * 64
LEGACY = "events.jsonl"


def _canonical(event: Dict[str, Any]) -> bytes:
    body = {k: v for k, v in event.items() if k != "hash"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def event_hash(event: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(event)).hexdigest()


class Journal:
    def __init__(self, journal_dir: Path):
        self.dir = Path(journal_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.legacy_path = self.dir / LEGACY
        self._lock = threading.Lock()
        self._lockfile = self.dir / ".lock"

    # ---- files ---------------------------------------------------------------
    def shards(self) -> List[Path]:
        """All journal files in replay order: legacy first, then by month."""
        out = [self.legacy_path] if self.legacy_path.exists() else []
        return out + sorted(self.dir.glob("events-*.jsonl"))

    def _shard_for(self, ts: str) -> Path:
        return self.dir / f"events-{ts[:7]}.jsonl"

    @property
    def path(self) -> Path:
        """The file the next event goes to."""
        return self._shard_for(utcnow_iso())

    @contextmanager
    def _locked(self):
        with self._lock:
            if fcntl is None:
                yield
                return
            with open(self._lockfile, "a") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _head(self) -> str:
        """Hash the next event must chain to (call with the lock held)."""
        for shard in reversed(sorted(self.dir.glob("events-*.jsonl"))):
            last = _last_line(shard)
            if last:
                return json.loads(last)["hash"]
        return self._legacy_anchor()

    def _legacy_anchor(self) -> str:
        if self.legacy_path.exists() and self.legacy_path.stat().st_size:
            return hashlib.sha256(self.legacy_path.read_bytes()).hexdigest()
        return GENESIS

    # ---- write ---------------------------------------------------------------
    def append(self, op: str, writer: str, memory_id: Optional[str] = None,
               mem: Optional[Memory] = None, **extra: Any) -> Dict[str, Any]:
        event: Dict[str, Any] = {"ts": utcnow_iso(), "writer": writer, "op": op,
                                 "memory_id": memory_id or (mem.id if mem else None)}
        if mem is not None:
            event["content_hash"] = mem.hash
            event["meta_hash"] = mem.meta_hash
            event["state"] = mem.to_json()
        event.update({k: v for k, v in extra.items() if v is not None})
        with self._locked():
            event["prev_hash"] = self._head()
            event["hash"] = event_hash(event)
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            with open(self._shard_for(event["ts"]), "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return event

    # ---- read ----------------------------------------------------------------
    def events(self, until: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        """Every event in order; with `until`, only those with ts <= until."""
        with self._lock:
            files = [p.read_text(encoding="utf-8") for p in self.shards()]
        for text in files:
            for line in text.splitlines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                if until is not None and ev["ts"] > until:
                    return
                yield ev

    def verify(self) -> Dict[str, Any]:
        """Walk the hash chain. Returns {ok, events, legacy_events, head, error}
        where `error` names the first broken shard:line."""
        legacy = 0
        if self.legacy_path.exists():
            legacy = sum(1 for ln in self.legacy_path.read_text(encoding="utf-8").splitlines()
                         if ln.strip())
        expected = self._legacy_anchor()
        count = 0
        for shard in sorted(self.dir.glob("events-*.jsonl")):
            for n, line in enumerate(shard.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                where = f"{shard.name}:{n}"
                try:
                    ev = json.loads(line)
                except ValueError:
                    return _bad(count, legacy, where, "line is not valid JSON")
                if ev.get("prev_hash") != expected:
                    return _bad(count, legacy, where,
                                "prev_hash does not match the previous event "
                                "(a line was removed, reordered, or inserted)")
                if ev.get("hash") != event_hash(ev):
                    return _bad(count, legacy, where,
                                "hash does not match the event's content (it was edited)")
                expected = ev["hash"]
                count += 1
        return {"ok": True, "events": count, "legacy_events": legacy, "head": expected,
                "error": None}


def _bad(count: int, legacy: int, where: str, why: str) -> Dict[str, Any]:
    return {"ok": False, "events": count, "legacy_events": legacy, "head": None,
            "error": f"{where}: {why}"}


def _last_line(path: Path) -> Optional[str]:
    """Last non-empty line of a file without reading all of it."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        data = b""
        while end > 0:
            start = max(0, end - 4096)
            f.seek(start)
            data = f.read(end - start) + data
            lines = [ln for ln in data.split(b"\n") if ln.strip()]
            if len(lines) > 1 or start == 0:
                return lines[-1].decode("utf-8") if lines else None
            end = start
    return None


def replay_states(events) -> Dict[str, Memory]:
    """Fold a stream of events into id -> Memory (the state the journal says
    should be on disk). Deleted memories are absent."""
    out: Dict[str, Memory] = {}
    for ev in events:
        mid = ev.get("memory_id")
        if not mid:
            continue
        if ev["op"] in DELETE_OPS:
            out.pop(mid, None)
        elif "state" in ev:
            out[mid] = memory_from_state(ev["state"])
    return out


def last_hashes(events) -> Dict[str, Tuple[str, str]]:
    """id -> (content_hash, meta_hash) of each live memory per the journal."""
    out: Dict[str, Tuple[str, str]] = {}
    for ev in events:
        mid = ev.get("memory_id")
        if not mid:
            continue
        if ev["op"] in DELETE_OPS:
            out.pop(mid, None)
        elif "state" in ev:
            out[mid] = (ev.get("content_hash"), ev.get("meta_hash"))
    return out


def memory_from_state(state: Dict[str, Any]) -> Memory:
    """Inverse of Memory.to_json (unknown keys are ignored, like the store)."""
    clean = {k: v for k, v in state.items() if k in Memory.__dataclass_fields__}
    clean["type"] = MemoryType.coerce(clean.get("type", "semantic"))
    return Memory(**clean)
