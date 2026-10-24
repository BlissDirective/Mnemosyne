"""Phase 2 (v0.5.0): memory rings, quarantine, and the promotion workflow."""

import shutil

import pytest

from mnemosyne import rings
from mnemosyne.config import Config, WriterSpec
from mnemosyne.engine import MemoryEngine
from mnemosyne.schema import Memory

ROSTER = {"owner": WriterSpec(3), "claude-code": WriterSpec(2, teams=["ops"]),
          "librarian": WriterSpec(2), "grok-sales-1": WriterSpec(1, teams=["sales"]),
          "grok-sales-2": WriterSpec(1, teams=["sales"]), "stranger": WriterSpec(0)}


def make_engine(tmp, **overrides) -> MemoryEngine:
    cfg = Config(root=tmp, provider="dev", writers=dict(ROSTER), **overrides)
    cfg.ensure_dirs()
    return MemoryEngine(cfg)


def ids(hits):
    return {h.memory.id for h in hits}


# --------------------------------------------------------------------------- #
# definition of done
# --------------------------------------------------------------------------- #
def test_tier1_canon_write_is_quarantined_until_promoted(tmp_path):
    eng = make_engine(tmp_path)
    mem = eng.remember("Our refund window is 60 days.", type="procedural",
                       ring="canon", writer="grok-sales-1")
    assert mem.ring == "proposed"
    assert mem.proposed_by == "grok-sales-1" and mem.proposed_target == "canon"
    assert list((tmp_path / "notes" / "proposed").glob(f"{mem.id}__*.md"))

    # invisible to everyone's default recall (including the reviewer's)
    for who in ("owner", "claude-code", "grok-sales-2", "grok-sales-1"):
        assert mem.id not in ids(eng.recall("refund window", writer=who)), who
    # reviewers can look at the queue on purpose
    assert mem.id in ids(eng.recall("refund window", writer="claude-code",
                                    scope=["proposed"]))

    # promotion requires a different, senior writer
    with pytest.raises(PermissionError, match="tier >= 2"):
        eng.promote(mem.id, writer="grok-sales-2")
    eng2_mem = eng.remember("Discounts above 20% need owner sign-off.",
                            ring="proposed", writer="claude-code")
    with pytest.raises(PermissionError, match="cannot promote it"):
        eng.promote(eng2_mem.id, writer="claude-code")

    promoted = eng.promote(mem.id, writer="claude-code")
    assert promoted.ring == "canon" and promoted.promoted_by == "claude-code"
    assert eng.store.path_by_id(mem.id).parent == tmp_path / "notes" / "canon"
    for who in ("owner", "grok-sales-2", "grok-sales-1"):
        assert mem.id in ids(eng.recall("refund window", writer=who)), who

    # every transition is journaled
    ops = [(e["op"], e["writer"]) for e in eng.journal.events() if e["memory_id"] == mem.id]
    assert ops == [("remember", "grok-sales-1"), ("promote", "claude-code")]
    eng.close()


def test_ring_survives_reindex(tmp_path):
    eng = make_engine(tmp_path)
    a = eng.remember("Canon fact about Globex.", ring="canon", writer="owner")
    b = eng.remember("Sales team note about Globex.", ring="team:sales",
                     writer="grok-sales-1")
    c = eng.remember("Private hunch about Globex.", writer="grok-sales-2")
    eng.close()
    shutil.rmtree(tmp_path / ".index")

    eng = make_engine(tmp_path)
    assert eng.sync()["embedded"] == 3
    assert {r: n for r, n in eng.index.stats()["by_ring"].items()} == {
        "canon": 1, "team:sales": 1, "private:grok-sales-2": 1}
    assert ids(eng.recall("Globex", writer="grok-sales-1")) == {a.id, b.id}
    assert ids(eng.recall("Globex", writer="grok-sales-2")) == {a.id, b.id, c.id}
    assert ids(eng.recall("Globex", writer="claude-code")) == {a.id}
    eng.close()


# --------------------------------------------------------------------------- #
# rings on disk and the write policy
# --------------------------------------------------------------------------- #
def test_filesystem_mirrors_rings(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("A.", ring="canon", writer="owner")
    eng.remember("B.", ring="team:sales", writer="grok-sales-1")
    eng.remember("C.", writer="claude-code")
    eng.remember("D.", ring="canon", writer="stranger")
    notes = tmp_path / "notes"
    assert len(list((notes / "canon").glob("*.md"))) == 1
    assert len(list((notes / "team" / "sales").glob("*.md"))) == 1
    assert len(list((notes / "private" / "claude-code").glob("*.md"))) == 1
    assert len(list((notes / "proposed").glob("*.md"))) == 1
    eng.close()


def test_write_policy(tmp_path):
    eng = make_engine(tmp_path)
    # default ring is the writer's own private ring
    assert eng.remember("x1", writer="grok-sales-1").ring == "private:grok-sales-1"
    # tier 1 may write its own team; another team is quarantined
    assert eng.remember("x2", ring="team:sales", writer="grok-sales-1").ring == "team:sales"
    q = eng.remember("x3", ring="team:ops", writer="grok-sales-1")
    assert q.ring == "proposed" and q.proposed_target == "team:ops"
    # tier 2 writes canon and any team directly
    assert eng.remember("x4", ring="canon", writer="claude-code").ring == "canon"
    assert eng.remember("x5", ring="team:sales", writer="librarian").ring == "team:sales"
    # tier 0 cannot even write its team directly
    assert eng.remember("x6", ring="team:sales", writer="stranger").ring == "proposed"
    # someone else's private ring: tier 3 only
    with pytest.raises(PermissionError, match="private ring needs tier 3"):
        eng.remember("x7", ring="private:owner", writer="claude-code")
    assert eng.remember("x8", ring="private:claude-code", writer="owner").ring == \
        "private:claude-code"
    with pytest.raises(ValueError, match="invalid ring"):
        eng.remember("x9", ring="public", writer="owner")
    eng.close()


def test_low_tier_cannot_edit_or_delete_shared_truth(tmp_path):
    eng = make_engine(tmp_path)
    fact = eng.remember("Pricing: Pro plan is $19/month.", ring="canon", writer="owner")
    with pytest.raises(PermissionError, match="memory_propose"):
        eng.update(fact.id, text="Pricing: Pro plan is $0/month.", writer="grok-sales-1")
    with pytest.raises(PermissionError):
        eng.forget(fact.id, writer="grok-sales-1")
    assert eng.get(fact.id).text == "Pricing: Pro plan is $19/month."
    # but they can edit their own private notes and their own proposals
    own = eng.remember("my note", writer="grok-sales-1")
    assert eng.update(own.id, text="my better note", writer="grok-sales-1")
    prop = eng.remember("proposal", ring="canon", writer="grok-sales-1")
    assert eng.update(prop.id, text="proposal v2", writer="grok-sales-1")
    with pytest.raises(PermissionError):
        eng.update(prop.id, text="hijack", writer="grok-sales-2")
    eng.close()


def test_read_policy(tmp_path):
    eng = make_engine(tmp_path)
    secret = eng.remember("Grok-sales-1's private lead list.", writer="grok-sales-1")
    assert secret.id not in ids(eng.recall("lead list", writer="grok-sales-2", scope=["all"]))
    assert secret.id not in ids(eng.recall("lead list", writer="claude-code", scope=["all"]))
    with pytest.raises(PermissionError, match="cannot read private:grok-sales-1"):
        eng.recall("lead list", writer="claude-code", scope=["private:grok-sales-1"])
    with pytest.raises(PermissionError, match="cannot read proposed"):
        eng.recall("lead list", writer="grok-sales-2", scope=["proposed"])
    # the owner (tier 3) can audit any ring
    assert secret.id in ids(eng.recall("lead list", writer="owner",
                                       scope=["private:grok-sales-1"]))
    assert secret.id in ids(eng.recall("lead list", writer="owner", scope=["all"]))
    eng.close()


# --------------------------------------------------------------------------- #
# propose / promote / reject
# --------------------------------------------------------------------------- #
def test_propose_private_note_to_canon(tmp_path):
    eng = make_engine(tmp_path)
    note = eng.remember("Initech's buyer is Dana Lee.", type="world_fact",
                        entities=["Initech"], writer="grok-sales-1")
    prop = eng.propose(note.id, writer="grok-sales-1")
    assert prop.ring == "proposed" and prop.proposed_from == note.id
    assert prop.entities == ["Initech"] and prop.writer == "grok-sales-1"
    eng.promote(prop.id, writer="claude-code")
    # now shared, and the private original is untouched
    assert prop.id in ids(eng.recall("Initech buyer", writer="grok-sales-2"))
    assert eng.get(note.id).ring == "private:grok-sales-1"
    assert eng.get(note.id).duplicate_of is None
    eng.close()


def test_proposed_correction_supersedes_canon_original(tmp_path):
    eng = make_engine(tmp_path)
    orig = eng.remember("Support hours are 9-5 ET.", ring="canon", writer="owner")
    fix = eng.propose(orig.id, text="Support hours are 8-6 ET.", writer="grok-sales-1")
    eng.promote(fix.id, writer="claude-code")
    assert eng.get(orig.id).duplicate_of == fix.id
    texts = [h.memory.text for h in eng.recall("support hours", writer="grok-sales-1")]
    assert "Support hours are 8-6 ET." in texts
    assert "Support hours are 9-5 ET." not in texts
    ops = [e["op"] for e in eng.journal.events()]
    assert ops[-2:] == ["promote", "supersede"]
    eng.close()


def test_reject_returns_to_proposer_with_reason(tmp_path):
    eng = make_engine(tmp_path)
    prop = eng.remember("The CEO said we can skip QA.", ring="canon", writer="grok-sales-1")
    with pytest.raises(PermissionError):
        eng.reject(prop.id, "nope", writer="grok-sales-2")
    with pytest.raises(ValueError, match="needs a reason"):
        eng.reject(prop.id, "  ", writer="claude-code")
    out = eng.reject(prop.id, "unverified hearsay", writer="claude-code")
    assert out.ring == "private:grok-sales-1"
    assert "rejected: unverified hearsay" in out.tags
    assert eng.store.path_by_id(prop.id).parent.name == "grok-sales-1"
    # it stays out of shared recall, but the proposer still sees it
    assert prop.id not in ids(eng.recall("skip QA", writer="claude-code"))
    assert prop.id in ids(eng.recall("skip QA", writer="grok-sales-1"))
    # only proposals can be promoted/rejected
    with pytest.raises(ValueError, match="not in the proposed"):
        eng.promote(prop.id, writer="claude-code")
    eng.close()


def test_prime_shows_review_queue_to_reviewers(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("Canon fact.", ring="canon", writer="owner")
    p1 = eng.remember("Proposal one.", ring="canon", writer="grok-sales-1")
    eng.remember("Proposal two.", ring="canon", writer="grok-sales-2")
    out = eng.prime(writer="claude-code")
    assert "[review] 2 proposal(s) awaiting review" in out
    assert p1.id in out and "Canon fact." in out
    worker = eng.prime(writer="grok-sales-1")
    assert "[review]" not in worker and "Proposal one." not in worker
    eng.close()


# --------------------------------------------------------------------------- #
# safety of consolidation and legacy stores
# --------------------------------------------------------------------------- #
def test_reflect_never_merges_or_corroborates_across_rings(tmp_path):
    eng = make_engine(tmp_path)
    canon = eng.remember("The user lives in Berlin.", type="world_fact",
                         ring="canon", writer="owner")
    for i in range(3):  # a low-trust agent repeating itself privately
        eng.remember("The user lives in Berlin.", type="world_fact", writer="grok-sales-1")
    before = eng.get(canon.id).confidence
    report = eng.reflect(writer="librarian")
    assert canon.id not in report.reinforced
    assert eng.get(canon.id).duplicate_of is None
    assert eng.get(canon.id).confidence == before
    # the private copies did dedup among themselves
    assert any(len(c) == 3 for c in report.duplicate_clusters)
    with pytest.raises(PermissionError, match="tier >= 2"):
        eng.reflect(writer="grok-sales-1")
    eng.close()


def test_legacy_ringless_notes_read_as_canon(tmp_path):
    eng = make_engine(tmp_path)
    legacy = Memory(text="Legacy single-user fact.")
    raw = legacy.to_markdown().replace("ring: canon\n", "")
    assert "ring:" not in raw
    (tmp_path / "notes" / f"{legacy.id}__legacy.md").write_text(raw)
    eng.sync()
    assert legacy.id in ids(eng.recall("legacy fact", writer="grok-sales-1"))
    eng.update(legacy.id, confidence=0.9, writer="owner")
    assert eng.store.path_by_id(legacy.id).parent == tmp_path / "notes" / "canon"
    assert not (tmp_path / "notes" / f"{legacy.id}__legacy.md").exists()
    eng.close()


def test_scope_resolution():
    from mnemosyne.auth import Identity
    worker = Identity("grok-sales-1", 1, ("sales",))
    assert rings.resolve_scope(worker, None) == {
        "canon", "private:grok-sales-1", "team:sales"}
    assert rings.resolve_scope(worker, ["all"], ["team:ops", "proposed"]) == {
        "canon", "private:grok-sales-1", "team:sales"}
    senior = Identity("claude-code", 2)
    assert "proposed" in rings.resolve_scope(senior, ["all"], ["proposed", "team:ops"])
    assert rings.ring_dir("team:sales").as_posix() == "team/sales"
