"""Phase 6 (v0.9.0): bitemporal event sourcing — hash-chained journal,
replay, as-of recall."""

import json
from pathlib import Path

import pytest

from mnemosyne import journal as journal_mod
from mnemosyne.config import Config, WriterSpec
from mnemosyne.engine import MemoryEngine

ROSTER = {"owner": WriterSpec(3), "claude-code": WriterSpec(2), "librarian": WriterSpec(2),
          "grok-sales-1": WriterSpec(1, teams=["sales"])}


def make_engine(tmp, **overrides) -> MemoryEngine:
    cfg = Config(root=tmp, provider="dev", writers=dict(ROSTER), **overrides)
    cfg.ensure_dirs()
    return MemoryEngine(cfg)


class Clock:
    """Drive the journal's timestamps so tests can plant history."""

    def __init__(self, monkeypatch, start="2026-09-01T09:00:00+00:00"):
        self.now = start
        monkeypatch.setattr(journal_mod, "utcnow_iso", lambda: self.now)

    def set(self, ts):
        self.now = ts


def note_files(root: Path):
    notes = root / "notes"
    return {p.relative_to(notes).as_posix(): p.read_bytes()
            for p in notes.rglob("*.md")
            if not any(part.startswith("_") for part in p.relative_to(notes).parts)}


def busy_store(eng):
    """Exercise every kind of event."""
    a = eng.remember("Acme pays $500 per month.", type="world_fact", entities=["Acme"],
                     ring="canon", writer="owner")
    b = eng.remember("Acme pays $500 per month.", type="world_fact", entities=["Acme"],
                     ring="canon", writer="claude-code")
    q = eng.remember("Initech wants SSO.", ring="canon", writer="grok-sales-1")
    eng.promote(q.id, writer="claude-code")
    h = eng.remember("Annual billing lifts retention.", type="hypothesis", ring="canon",
                     writer="claude-code")
    eng.resolve(h.id, "confirmed", writer="owner")
    eng.update(a.id, tags=["billing"], writer="owner")
    eng.relate(q.id, "supports", a.id, writer="claude-code")
    eng.recall("Acme")
    eng.reflect(writer="librarian")
    junk = eng.remember("Temporary.", writer="owner")
    eng.forget(junk.id, writer="owner")
    eng.flush_access()
    return a, b, q, h


# --------------------------------------------------------------------------- #
# definition of done
# --------------------------------------------------------------------------- #
@pytest.mark.files_only
def test_replay_round_trips_byte_for_byte(tmp_path):
    eng = make_engine(tmp_path / "store")
    busy_store(eng)
    out = eng.replay(tmp_path / "replayed")
    assert out["memories"] == len(note_files(tmp_path / "store"))
    assert note_files(tmp_path / "replayed") == note_files(tmp_path / "store")
    # the derived reputation pages come back too
    assert (tmp_path / "replayed/notes/canon/_writers/claude-code.md").read_text() == \
        (tmp_path / "store/notes/canon/_writers/claude-code.md").read_text()
    with pytest.raises(ValueError, match="empty directory"):
        eng.replay(tmp_path / "replayed")
    eng.close()


def test_as_of_recall_returns_belief_state_at_a_planted_moment(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    eng = make_engine(tmp_path)
    clock.set("2026-09-01T09:00:00+00:00")
    price = eng.remember("Acme pays $500 per month.", type="world_fact", entities=["Acme"],
                         ring="canon", writer="owner")
    clock.set("2026-09-10T09:00:00+00:00")
    decision = eng.remember("Decision: keep Acme on the legacy plan.", type="decision",
                            entities=["Acme"], ring="canon", writer="owner")
    clock.set("2026-10-05T09:00:00+00:00")
    eng.update(price.id, text="Acme pays $900 per month.", writer="owner")
    clock.set("2026-10-06T09:00:00+00:00")
    late = eng.remember("Acme is moving to the enterprise plan.", entities=["Acme"],
                        ring="canon", writer="owner")

    def texts(hits):
        return {h.memory.text for h in hits}

    # "what did we believe on the day we decided X?"
    then = eng.recall("Acme price plan", entities=["Acme"], as_of="2026-09-10")
    assert "Acme pays $500 per month." in texts(then)
    assert decision.id in {h.memory.id for h in then}
    assert late.id not in {h.memory.id for h in then}
    assert "Acme pays $900 per month." not in texts(then)
    # before anything was known
    assert eng.recall("Acme", entities=["Acme"], as_of="2026-08-31") == []
    # now
    now = eng.recall("Acme price plan", entities=["Acme"])
    assert "Acme pays $900 per month." in texts(now) and late.id in {h.memory.id for h in now}

    # read-only: the snapshot can't be written, and the live store is untouched
    snap = eng._snapshot("2026-09-10")
    with pytest.raises(PermissionError, match="read-only"):
        snap.remember("sneaky", writer="owner")
    assert eng.get(price.id).text == "Acme pays $900 per month."
    eng.close()


@pytest.mark.files_only
def test_hash_chain_catches_a_tampered_line(tmp_path):
    eng = make_engine(tmp_path)
    busy_store(eng)
    ok = eng.verify_journal()
    assert ok["ok"] and ok["events"] > 10 and len(ok["head"]) == 64

    shard = sorted((tmp_path / "journal").glob("events-*.jsonl"))[-1]
    pristine = shard.read_text()
    lines = pristine.splitlines()

    # 1. edit a past event's content (rewrite history)
    ev = json.loads(lines[2])
    ev["state"]["text"] = "Acme pays $0 per month."
    shard.write_text("\n".join(lines[:2] + [json.dumps(ev)] + lines[3:]) + "\n")
    bad = eng.verify_journal()
    assert not bad["ok"] and f"{shard.name}:3" in bad["error"] and "edited" in bad["error"]

    # 2. even with a recomputed hash, the NEXT event's prev_hash exposes it
    ev["hash"] = journal_mod.event_hash(ev)
    shard.write_text("\n".join(lines[:2] + [json.dumps(ev)] + lines[3:]) + "\n")
    bad = eng.verify_journal()
    assert not bad["ok"] and f"{shard.name}:4" in bad["error"]

    # 3. delete a line
    shard.write_text("\n".join(lines[:4] + lines[5:]) + "\n")
    assert "removed, reordered" in eng.verify_journal()["error"]
    # 4. reorder two lines
    shard.write_text("\n".join(lines[:4] + [lines[5], lines[4]] + lines[6:]) + "\n")
    assert not eng.verify_journal()["ok"]

    shard.write_text(pristine)
    assert eng.verify_journal()["ok"]
    eng.close()


# --------------------------------------------------------------------------- #
# completeness of the record
# --------------------------------------------------------------------------- #
@pytest.mark.files_only
def test_out_of_band_edits_are_journaled_so_replay_stays_exact(tmp_path):
    eng = make_engine(tmp_path / "store")
    a, b, q, h = busy_store(eng)
    # a hand edit and a hand deletion (e.g. arriving via git pull)
    p = eng.store.path_by_id(q.id)
    p.write_text(p.read_text().replace("Initech wants SSO.", "Initech wants SSO and SCIM."))
    eng.store.path_by_id(h.id).unlink()
    result = eng.sync()
    assert result["journaled_external"] == 2
    ops = [(e["op"], e["writer"]) for e in eng.journal.events()][-2:]
    assert ("external", "external") in ops and ("external_delete", "external") in ops
    assert eng.sync()["journaled_external"] == 0, "reconciliation must be idempotent"
    eng.replay(tmp_path / "replayed")
    assert note_files(tmp_path / "replayed") == note_files(tmp_path / "store")
    assert eng.verify_journal()["ok"]
    eng.close()


@pytest.mark.files_only
def test_monthly_shards_and_legacy_anchor(tmp_path, monkeypatch):
    # a store from v0.4-v0.8 has an unchained journal/events.jsonl
    legacy_eng = make_engine(tmp_path)
    m = legacy_eng.remember("Pre-chain memory.", ring="canon", writer="owner")
    legacy_eng.close()
    shard = next((tmp_path / "journal").glob("events-*.jsonl"))
    legacy_lines = []
    for line in shard.read_text().splitlines():
        ev = json.loads(line)
        ev.pop("prev_hash"), ev.pop("hash")
        legacy_lines.append(json.dumps(ev))
    shard.unlink()
    (tmp_path / "journal" / "events.jsonl").write_text("\n".join(legacy_lines) + "\n")

    clock = Clock(monkeypatch, "2026-09-30T23:00:00+00:00")
    eng = make_engine(tmp_path)
    eng.update(m.id, tags=["sept"], writer="owner")
    clock.set("2026-10-01T01:00:00+00:00")
    eng.update(m.id, tags=["oct"], writer="owner")
    names = sorted(p.name for p in (tmp_path / "journal").glob("events*.jsonl"))
    assert names == ["events-2026-09.jsonl", "events-2026-10.jsonl", "events.jsonl"]
    v = eng.verify_journal()
    assert v["ok"] and v["legacy_events"] == len(legacy_lines) and v["events"] == 2
    # replay reads legacy first, then the shards in order
    assert eng.replay(tmp_path / "out")["memories"] == 1
    assert eng.get(m.id).tags == ["oct"]
    # tampering with the legacy prefix is caught through the anchor
    lp = tmp_path / "journal" / "events.jsonl"
    lp.write_text(lp.read_text().replace("Pre-chain", "Post-chain"))
    assert not eng.verify_journal()["ok"]
    eng.close()


def test_as_of_judges_validity_at_that_date(tmp_path, monkeypatch):
    """Bitemporal: valid_from/valid_to (did the fact hold?) are judged at the
    as-of date, separately from journal time (did we know it?)."""
    clock = Clock(monkeypatch, "2026-09-01T09:00:00+00:00")
    eng = make_engine(tmp_path)
    old = eng.remember("Dana is Initech's buyer.", type="world_fact", entities=["Initech"],
                       valid_to="2026-09-15", ring="canon", writer="owner")
    new = eng.remember("Lee is Initech's buyer.", type="world_fact", entities=["Initech"],
                       valid_from="2026-09-16", ring="canon", writer="owner")
    clock.set("2026-10-20T09:00:00+00:00")
    ids = lambda hits: {h.memory.id for h in hits}
    assert ids(eng.recall("Initech buyer", entities=["Initech"], as_of="2026-09-10")) == {old.id}
    assert ids(eng.recall("Initech buyer", entities=["Initech"], as_of="2026-10-01")) == {new.id}
    eng.close()


def test_snapshots_are_cached_and_invalidated(tmp_path, monkeypatch):
    clock = Clock(monkeypatch, "2026-09-01T09:00:00+00:00")
    eng = make_engine(tmp_path)
    eng.remember("First.", ring="canon", writer="owner")
    s1 = eng._snapshot("2026-09-02")
    assert eng._snapshot("2026-09-02") is s1
    assert (tmp_path / ".index" / "asof").exists(), "snapshots are derived, under .index"
    clock.set("2026-09-01T10:00:00+00:00")  # a late-arriving event inside the window
    eng.remember("Second.", ring="canon", writer="owner")
    s2 = eng._snapshot("2026-09-02")
    assert s2 is not s1 and s2.store.count() == 2
    with pytest.raises(ValueError, match="ISO date"):
        eng.recall("x", as_of="last tuesday")
    eng.close()
