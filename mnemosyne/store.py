"""The canonical layer: memories as Markdown files on disk.

This is the source of truth. The index (index.py) is derived from it and can be
rebuilt at any time. Filenames are `<id>__<slug>.md` so a human browsing the
folder (or Obsidian) sees meaningful names.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .schema import Memory, slugify, utcnow_iso, content_hash


class MarkdownStore:
    def __init__(self, notes_dir: Path, archive_dir: Path):
        self.notes_dir = Path(notes_dir)
        self.archive_dir = Path(archive_dir)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    # ---- filenames -------------------------------------------------------
    def _path_for(self, mem: Memory) -> Path:
        return self.notes_dir / f"{mem.id}__{slugify(mem.text)}.md"

    def path_by_id(self, mem_id: str) -> Optional[Path]:
        matches = list(self.notes_dir.glob(f"{mem_id}__*.md"))
        if not matches:
            # fall back to any file whose frontmatter id matches
            for p in self.notes_dir.glob("*.md"):
                if p.stem.startswith(mem_id):
                    return p
        return matches[0] if matches else None

    # ---- CRUD ------------------------------------------------------------
    def write(self, mem: Memory) -> Path:
        """Create or overwrite the memory's file (renaming if the slug changed)."""
        existing = self.path_by_id(mem.id)
        target = self._path_for(mem)
        _atomic_write(target, mem.to_markdown())
        if existing and existing != target:
            existing.unlink(missing_ok=True)
        return target

    def read(self, path: Path) -> Memory:
        return Memory.from_markdown(Path(path).read_text(encoding="utf-8"))

    def read_by_id(self, mem_id: str) -> Optional[Memory]:
        p = self.path_by_id(mem_id)
        return self.read(p) if p else None

    def delete(self, mem_id: str) -> bool:
        p = self.path_by_id(mem_id)
        if p and p.exists():
            p.unlink()
            return True
        return False

    def iter_memories(self) -> Iterator[Memory]:
        for p in sorted(self.notes_dir.glob("*.md")):
            try:
                yield self.read(p)
            except Exception:
                # a malformed file should never break the whole store
                continue

    def count(self) -> int:
        return sum(1 for _ in self.notes_dir.glob("*.md"))

    # ---- verbatim archive ------------------------------------------------
    def archive_raw(self, text: str, label: str = "session") -> Path:
        """Store raw, unsummarized source (e.g. a transcript chunk) so nothing is
        ever truly lost, even when the distilled memory is compact. Returns the
        absolute path; the engine records it *relative to the memory root* so
        the reference survives Git sync to other machines."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        h = content_hash(text)
        path = self.archive_dir / f"{ts}__{slugify(label)}__{h}.md"
        header = f"<!-- archived {utcnow_iso()} | label={label} -->\n\n"
        path.write_text(header + text.strip() + "\n", encoding="utf-8")
        return path


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename so a concurrent reader (another writer's
    request, a sync tool, `git add`) never sees a half-written memory."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md.part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
