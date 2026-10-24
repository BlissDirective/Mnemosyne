"""The audit journal: one JSON line per mutation, append-only.

`journal/events.jsonl` sits next to `notes/` and is canonical (Git-synced like
the notes): it is the record of who changed what, when. Every event carries

    ts, writer, op, memory_id, content_hash, meta_hash

and, for anything that leaves a memory on disk, `state` — the memory's full
frontmatter + text after the change. That makes the trail complete enough to
replay: applying the events in order reproduces the current notes for every
memory the journal covers (`replay_states`).

Append-only is a convention enforced by the code (nothing here rewrites or
truncates the file) and verified by tests.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .schema import Memory, MemoryType, utcnow_iso

# ops that leave the memory present on disk (and so carry `state`)
STATE_OPS = {"remember", "update", "reflect", "access",
             "propose", "promote", "reject", "supersede", "relate"}
DELETE_OPS = {"forget"}


class Journal:
    def __init__(self, journal_dir: Path):
        self.dir = Path(journal_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"
        self._lock = threading.Lock()

    def append(self, op: str, writer: str, memory_id: Optional[str] = None,
               mem: Optional[Memory] = None, **extra: Any) -> Dict[str, Any]:
        event: Dict[str, Any] = {"ts": utcnow_iso(), "writer": writer, "op": op,
                                 "memory_id": memory_id or (mem.id if mem else None)}
        if mem is not None:
            event["content_hash"] = mem.hash
            event["meta_hash"] = mem.meta_hash
            event["state"] = mem.to_json()
        event.update({k: v for k, v in extra.items() if v is not None})
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return event

    def events(self) -> Iterator[Dict[str, Any]]:
        if not self.path.exists():
            return
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.strip():
                yield json.loads(line)


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


def memory_from_state(state: Dict[str, Any]) -> Memory:
    """Inverse of Memory.to_json (unknown keys are ignored, like the store)."""
    clean = {k: v for k, v in state.items() if k in Memory.__dataclass_fields__}
    clean["type"] = MemoryType.coerce(clean.get("type", "semantic"))
    return Memory(**clean)
