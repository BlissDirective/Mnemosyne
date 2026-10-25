"""Import an organization's existing Markdown logs into memory.

Agent-run businesses usually start with "the filesystem is your memory":
a decision log and a state file the lead agent appends to. This importer turns
those into governed memories without losing a word:

  * each entry becomes ONE memory — a `decision` (decision logs) or an
    `episodic` status snapshot (state files) — written to canon by the
    configured writer, dated with `valid_from`, tagged `imported`;
  * the entry's full raw text is kept verbatim in the archive
    (`archive_text`), so the distilled memory never replaces the record;
  * it is idempotent: every entry carries `import:<hash>` and re-runs skip
    what is already there;
  * entries the secrets screen rejects are skipped and reported, never
    partially imported.

Recognized entry shapes (all three appear in real logs):

    2026-07-14 | decision | charter ref | rationale | tier | approval
    ## 2026-07-23 — REQ-20260723-01 APPROVED: ...        (+ body until next heading)
    - **Owner rulings (2026-10-16, later):** ...         (+ indented sub-bullets)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from ..guardrails import SecretRejected

_DATE = r"(\d{4}-\d{2}-\d{2})"
_PIPE_RE = re.compile(rf"^{_DATE}\s*\|\s*(.+)$")
_HEAD_RE = re.compile(rf"^#{{2,4}}\s+(?:\S+\s+)?.*?{_DATE}")
_BULLET_RE = re.compile(r"^- \*\*(.+?)\*\*")
_BULLET_DATE_RE = re.compile(_DATE)
_REF_RE = re.compile(r"\b(REQ-\d{8}-\d{2}|PR #\d+|#\d{2,4})\b")

MAX_TEXT = 1500


@dataclass
class Entry:
    date: str
    title: str
    body: str
    raw: str

    @property
    def key(self) -> str:
        return hashlib.sha256(self.raw.strip().encode("utf-8")).hexdigest()[:12]


@dataclass
class ImportReport:
    found: int = 0
    imported: List[str] = field(default_factory=list)
    already_present: int = 0
    skipped: List[Dict[str, str]] = field(default_factory=list)

    def to_json(self) -> Dict[str, object]:
        return {"found": self.found, "imported": len(self.imported),
                "already_present": self.already_present, "skipped": self.skipped}


def _start(line: str, undated: Optional[str] = None) -> Optional[str]:
    """The entry date if this line starts a new entry, else None. With
    `undated`, undated section headings start entries dated `undated`."""
    m = _PIPE_RE.match(line) or _HEAD_RE.match(line)
    if m:
        return m.group(1)
    if undated and re.match(r"^#{2,4}\s+\S", line):
        return undated
    m = _BULLET_RE.match(line)
    if m:
        d = _BULLET_DATE_RE.search(m.group(1))
        return d.group(1) if d else None
    return None


def parse(text: str, undated: Optional[str] = None) -> List[Entry]:
    """Split a log into dated entries. An entry runs from a dated pipe row,
    dated heading, or dated bold bullet to the next one (or the next
    heading), so undated bullets that follow a ruling stay with it.
    Undated sections become entries dated `undated` if given (state files:
    an undated "## Current phase" is the current state); otherwise they and
    the preamble are left out — `coverage()` reports how much."""
    lines = text.splitlines()
    starts = [(i, _start(ln, undated)) for i, ln in enumerate(lines)]
    entries: List[Entry] = []
    for i, day in starts:
        if not day:
            continue
        j = i + 1
        while j < len(lines) and not _start(lines[j], undated) \
                and not lines[j].startswith("#"):
            j += 1
        raw = "\n".join(lines[i:j]).rstrip()
        first = lines[i]
        if _PIPE_RE.match(first):
            fields = [f.strip() for f in _PIPE_RE.match(first).group(2).split("|")]
            title = fields[0]
            body = (" | ".join(fields[1:]) + "\n" + "\n".join(lines[i + 1:j])).strip()
        elif first.startswith("#"):
            title = re.sub(r"^#+\s*", "", first).strip()
            body = "\n".join(lines[i + 1:j]).strip()
        else:
            m = _BULLET_RE.match(first)
            title = m.group(1).rstrip(":")
            body = (first[m.end():].strip() + "\n" + "\n".join(lines[i + 1:j])).strip()
        entries.append(Entry(day, title, body, raw))
    return entries


def coverage(text: str, undated: Optional[str] = None) -> float:
    """Share of non-blank lines that ended up inside some entry."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    covered = sum(len([ln for ln in e.raw.splitlines() if ln.strip()])
                  for e in parse(text, undated))
    return covered / len(lines) if lines else 1.0


def _statement(e: Entry, kind: str) -> str:
    lead = "Decision" if kind == "decision" else "Status"
    flat = " ".join(f"{e.title}. {e.body}".split())
    text = f"{lead} ({e.date}): {flat}"
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def import_log(engine, text: str, kind: str = "decision", writer: str = "director",
               ring: str = "canon", source: str = "import", dry_run: bool = False,
               as_of: Optional[str] = None) -> ImportReport:
    """Import every entry of a log. kind: 'decision' or 'state'. For state
    files, undated sections are dated `as_of` (default: today)."""
    if kind not in ("decision", "state"):
        raise ValueError("kind must be 'decision' or 'state'")
    undated = (as_of or date.today().isoformat()) if kind == "state" else None
    entries = parse(text, undated)
    report = ImportReport(found=len(entries))
    present = {t for _id, tags in engine.index.ids_with_tag_prefix("import:")
               for t in tags if t.startswith("import:")}
    for e in entries:
        tag = f"import:{e.key}"
        if tag in present:
            report.already_present += 1
            continue
        if dry_run:
            report.imported.append(tag)
            continue
        refs = sorted(set(_REF_RE.findall(e.raw)))[:12]
        try:
            mem = engine.remember(
                _statement(e, kind), type="decision" if kind == "decision" else "episodic",
                entities=refs, tags=["imported", tag, f"log:{kind}"], source=source,
                confidence=0.8, valid_from=e.date, archive_text=e.raw,
                writer=writer, ring=ring)
            report.imported.append(mem.id)
            present.add(tag)
        except SecretRejected as err:
            report.skipped.append({"date": e.date, "title": e.title[:120],
                                   "reason": str(err).split(".")[0]})
    return report
