"""Core data model: a single unit of memory.

A Memory is stored on disk as one Markdown file: YAML frontmatter for the
structured fields, and the body for the memory text. This keeps every memory
human-readable, diff-able, and portable — the whole point of the canonical
layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import yaml


class MemoryType(str, Enum):
    """A small, unified taxonomy (after the CoALA cognitive-architecture split,
    plus two agent-specific kinds from Hindsight)."""

    EPISODIC = "episodic"        # a specific event/exchange ("on 2026-07-05 we decided X")
    SEMANTIC = "semantic"        # a general, durable fact learned over time
    PROCEDURAL = "procedural"    # how to do something / a workflow / a rule
    WORLD_FACT = "world_fact"    # a fact about the world ("Alice works at Google")
    EXPERIENCE = "experience"    # something the agent itself did ("I recommended Python")
    PREFERENCE = "preference"    # a stated preference of the user

    @classmethod
    def coerce(cls, value: str) -> "MemoryType":
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.SEMANTIC


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    """Sortable, collision-resistant id: UTC timestamp + short random suffix."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{secrets.token_hex(3)}"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return s[:max_len] or "memory"


@dataclass
class Memory:
    """One unit of memory. `text` is the source of truth; everything else is
    metadata used for retrieval and consolidation."""

    text: str
    type: MemoryType = MemoryType.SEMANTIC
    id: str = field(default_factory=new_id)
    created: str = field(default_factory=utcnow_iso)
    updated: str = field(default_factory=utcnow_iso)
    entities: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    source: str = "manual"                # channel: conversation / cli / import ...
    writer: str = "owner"                 # stable id of WHO wrote it (agent or human)
    confidence: float = 0.7
    valid_from: Optional[str] = None      # temporal validity (world facts)
    valid_to: Optional[str] = None
    links: List[str] = field(default_factory=list)   # ids of related memories
    duplicate_of: Optional[str] = None    # set by reflect() when merged
    archive_ref: Optional[str] = None     # path to verbatim source (relative to root)
    recall_count: int = 0                 # access counter: times returned by recall
    last_recalled: Optional[str] = None   # access counter: ISO timestamp of last recall
    # rings & review (see rings.py). Files written before rings existed were
    # the owner's single-user truth, so a missing ring reads as canon.
    ring: str = "canon"
    proposed_by: Optional[str] = None     # who put it in the review queue
    proposed_target: Optional[str] = None # ring it should land in if promoted
    proposed_from: Optional[str] = None   # id it was copied from (memory_propose by id)
    promoted_by: Optional[str] = None     # reviewer who promoted it
    promoted_at: Optional[str] = None

    # ---- convenience -----------------------------------------------------
    @property
    def hash(self) -> str:
        """Hash of the *text only* — decides whether re-embedding is needed."""
        return content_hash(self.text)

    @property
    def meta_hash(self) -> str:
        """Hash of the retrieval-relevant metadata — decides whether the index
        row needs a (cheap, no re-embed) metadata refresh. This is what lets
        frontmatter changes (confidence, links, duplicate_of, access counters)
        propagate to the index instead of going permanently stale."""
        payload = json.dumps(
            {
                "type": self.type.value,
                "updated": self.updated,
                "source": self.source,
                "writer": self.writer,
                "confidence": self.confidence,
                "valid_from": self.valid_from,
                "valid_to": self.valid_to,
                "tags": sorted(self.tags),
                "entities": sorted(self.entities),
                "links": sorted(self.links),
                "duplicate_of": self.duplicate_of,
                "recall_count": self.recall_count,
                "last_recalled": self.last_recalled,
                "ring": self.ring,
                "proposed_by": self.proposed_by,
                "proposed_target": self.proposed_target,
                "proposed_from": self.proposed_from,
                "promoted_by": self.promoted_by,
                "promoted_at": self.promoted_at,
            },
            sort_keys=True,
        )
        return content_hash(payload)

    def is_expired(self, today_iso: Optional[str] = None) -> bool:
        """True if this memory has a `valid_to` in the past (date-level compare)."""
        if not self.valid_to:
            return False
        today = (today_iso or utcnow_iso())[:10]
        return str(self.valid_to)[:10] < today

    def touch(self) -> None:
        self.updated = utcnow_iso()

    # ---- (de)serialization ----------------------------------------------
    def frontmatter(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        # keep frontmatter tidy: drop empties
        for k in ("entities", "tags", "links"):
            if not d[k]:
                d.pop(k)
        for k in ("valid_from", "valid_to", "duplicate_of", "archive_ref", "last_recalled",
                  "proposed_by", "proposed_target", "proposed_from", "promoted_by",
                  "promoted_at"):
            if d[k] is None:
                d.pop(k)
        if not d.get("recall_count"):
            d.pop("recall_count", None)
        text = d.pop("text")  # text lives in the body, not frontmatter
        return d, text

    def to_markdown(self) -> str:
        fm, _ = self.frontmatter()
        y = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
        return f"---\n{y}\n---\n\n{self.text.strip()}\n"

    @classmethod
    def from_markdown(cls, raw: str) -> "Memory":
        fm, body = _split_frontmatter(raw)
        fm = dict(fm or {})
        fm["type"] = MemoryType.coerce(fm.get("type", "semantic"))
        fm["text"] = body.strip()
        if "recall_count" in fm:
            try:
                fm["recall_count"] = int(fm["recall_count"])
            except (TypeError, ValueError):
                fm["recall_count"] = 0
        # ignore unknown keys gracefully
        allowed = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in fm.items() if k in allowed}
        return cls(**clean)

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        return d


def _split_frontmatter(raw: str):
    """Parse a `---`-delimited YAML frontmatter block. Returns (dict, body)."""
    raw = raw.lstrip("﻿")  # strip BOM if present
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            meta = yaml.safe_load(parts[1]) or {}
            return meta, parts[2].lstrip("\n")
    # no frontmatter: treat whole file as body
    return {}, raw
