"""Phase 4 (v0.7.0): outcome calibration and writer reputation."""

import shutil
from datetime import date, timedelta

import pytest

from mnemosyne import reputation
from mnemosyne.config import Config, WriterSpec
from mnemosyne.engine import MemoryEngine
from mnemosyne.schema import Memory

ROSTER = {"owner": WriterSpec(3), "claude-code": WriterSpec(2),
          "librarian": WriterSpec(2), "grok-good": WriterSpec(2),
          "grok-bad": WriterSpec(2), "grok-sales-1": WriterSpec(1)}


def make_engine(tmp, **overrides) -> MemoryEngine:
    cfg = Config(root=tmp, provider="dev", writers=dict(ROSTER), **overrides)
    cfg.ensure_dirs()
    return MemoryEngine(cfg)


def hyp(eng, text, writer, confidence=0.7, **kw):
    return eng.remember(text, type="hypothesis", ring="canon", writer=writer,
                        confidence=confidence, **kw)


# --------------------------------------------------------------------------- #
# definition of done
# --------------------------------------------------------------------------- #
@pytest.mark.files_only
def test_resolving_moves_writer_reputation(tmp_path):
    eng = make_engine(tmp_path)
    assert eng.reputations() == {}
    good = hyp(eng, "Annual plans will lift retention.", "grok-good", confidence=0.9)
    bad = hyp(eng, "A TikTok campaign will double signups.", "grok-bad", confidence=0.9)

    eng.resolve(good.id, "confirmed", note="Q3 retention +8pts", writer="owner")
    eng.resolve(bad.id, "refuted", note="signups +3%", writer="owner")
    reps = eng.reputations()
    assert reps["grok-good"]["reputation"] > reputation.NEUTRAL > reps["grok-bad"]["reputation"]
    assert reps["grok-good"]["resolved"] == 1

    # the canonical, owner-readable record exists and agrees
    page = (tmp_path / "notes" / "canon" / "_writers" / "grok-bad.md").read_text()
    assert "writer: grok-bad" in page and "refuted" in page and bad.id in page
    # and it is not mistaken for a memory
    assert all(m.id != "grok-bad" for m in eng.store.iter_memories())
    assert [e["op"] for e in eng.journal.events()][-1] == "resolve"
    eng.close()


def test_refuted_memory_leaves_recall_but_stays_auditable(tmp_path):
    eng = make_engine(tmp_path)
    h = hyp(eng, "Enterprise buyers want on-prem deployment.", "grok-good",
            entities=["enterprise"])
    out = eng.resolve(h.id, "refuted", note="0 of 12 asked", writer="claude-code")
    assert out.valid_to == date.today().isoformat() and "refuted" in out.tags
    assert out.resolution["forecast"] == 0.7 and out.resolution["by"] == "claude-code"
    assert h.id not in {x.memory.id for x in eng.recall("enterprise on-prem")}
    assert h.id in {x.memory.id for x in eng.recall("enterprise on-prem",
                                                    include_superseded=True)}
    assert eng.get(h.id) is not None, "refutation must never delete history"
    eng.close()


def test_reputation_affects_ranking_boundedly(tmp_path):
    eng = make_engine(tmp_path)
    # build track records: grok-good right 4/4, grok-bad wrong 4/4
    for i in range(4):
        g = hyp(eng, f"Good call number {i} about pricing tier {i}.", "grok-good", 0.9)
        b = hyp(eng, f"Bad call number {i} about churn driver {i}.", "grok-bad", 0.9)
        eng.resolve(g.id, "confirmed", writer="owner")
        eng.resolve(b.id, "refuted", writer="owner")
    reps = eng.reputations()
    # 4 confident hits: (4*0.01 + 2*0.25)/6 -> reputation 0.91 -> x1.16
    assert reps["grok-good"]["multiplier"] == pytest.approx(1.16)
    # 4 confident misses would be x0.63 unbounded; the floor holds it at x0.8
    assert reps["grok-bad"]["multiplier"] == pytest.approx(reputation.MULT_MIN)

    # two equally relevant, equally confident claims: they fuse to exactly
    # equal scores, and calibrated reputation decides
    claim_bad = eng.remember("The Globex deal closes in November.", ring="canon",
                             writer="grok-bad", entities=["Globex"])
    claim_good = eng.remember("The Globex deal closes in November.", ring="canon",
                              writer="grok-good", entities=["Globex"])
    hits = eng.recall("When does the Globex deal close?", entities=["Globex"], k=2)
    assert [h.memory.id for h in hits] == [claim_good.id, claim_bad.id]

    # bounded: reputation scales confidence by at most x0.8-1.2, and the whole
    # confidence term moves a fused score by only a few percent either way
    prior = eng._rank_prior()
    for w in ("grok-good", "grok-bad", "never-resolved"):
        for c in (0.0, 0.5, 1.0):
            assert 0.98 <= prior(Memory(text="x", writer=w, confidence=c)) <= 1.01
    same = lambda w: prior(Memory(text="x", writer=w, confidence=0.7))
    assert same("grok-good") > same("never-resolved") == 1.0 > same("grok-bad")
    # a strictly more relevant memory still beats reputation, even from the
    # worst-calibrated writer
    relevant = eng.remember("Globex deal close date: November 14, signed by Globex CFO.",
                            ring="canon", writer="grok-bad", entities=["Globex"])
    hits = eng.recall("Globex deal close date CFO signed", entities=["Globex"], k=1)
    assert hits[0].memory.id == relevant.id
    eng.close()


@pytest.mark.files_only
def test_reputations_survive_reindex(tmp_path):
    eng = make_engine(tmp_path)
    h = hyp(eng, "Bundling support lifts ARPU.", "grok-good", 0.8)
    eng.resolve(h.id, "confirmed", writer="owner")
    before = eng.reputations()
    page = tmp_path / "notes" / "canon" / "_writers" / "grok-good.md"
    page_text = page.read_text()
    eng.close()

    shutil.rmtree(tmp_path / ".index")
    page.unlink()                       # even the derived page can be lost
    eng = make_engine(tmp_path)
    eng.sync()
    assert eng.reputations() == before
    assert page.read_text() == page_text
    eng.close()


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
def test_resolution_rules(tmp_path):
    eng = make_engine(tmp_path)
    h = hyp(eng, "Self-serve onboarding cuts support tickets.", "claude-code")
    with pytest.raises(PermissionError, match="cannot grade it"):
        eng.resolve(h.id, "confirmed", writer="claude-code")
    with pytest.raises(PermissionError, match="tier >= 2"):
        eng.resolve(h.id, "confirmed", writer="grok-sales-1")
    with pytest.raises(ValueError, match="outcome must be"):
        eng.resolve(h.id, "probably", writer="owner")
    fact = eng.remember("Plain fact.", ring="canon", writer="owner")
    with pytest.raises(ValueError, match="only hypothesis or decision"):
        eng.resolve(fact.id, "confirmed", writer="claude-code")
    prop = eng.remember("Untested idea.", type="hypothesis", ring="canon",
                        writer="grok-sales-1")
    with pytest.raises(ValueError, match="still a proposal"):
        eng.resolve(prop.id, "confirmed", writer="claude-code")
    eng.resolve(h.id, "mixed", writer="librarian")
    assert eng.get(h.id).confidence == pytest.approx(0.6)
    with pytest.raises(ValueError, match="already resolved"):
        eng.resolve(h.id, "confirmed", writer="owner")
    # decisions can be resolved too (did it work out?)
    d = eng.remember("Decision: price Operator at $149.", type="decision", ring="canon",
                     writer="claude-code")
    eng.resolve(d.id, "confirmed", writer="owner")
    eng.close()


def test_corroboration_cannot_outrank_verified_outcome(tmp_path):
    eng = make_engine(tmp_path)
    verified = hyp(eng, "Customers churn after failed payments.", "grok-good", 0.7)
    eng.resolve(verified.id, "confirmed", writer="owner")
    loud = eng.remember("Customers churn because of the logo color.", ring="canon",
                        writer="claude-code", confidence=0.8)
    for i in range(6):  # repeat it over and over
        eng.remember("Customers churn because of the logo color.", ring="canon",
                     writer="owner", confidence=0.8)
    for _ in range(3):
        eng.reflect(writer="librarian")
    assert eng.get(loud.id).confidence <= 0.85
    assert eng.get(verified.id).confidence >= 0.9
    assert eng.get(verified.id).confidence > eng.get(loud.id).confidence
    eng.close()


def test_prime_lists_hypotheses_awaiting_resolution(tmp_path):
    eng = make_engine(tmp_path)
    past = (date.today() - timedelta(days=3)).isoformat()
    future = (date.today() + timedelta(days=30)).isoformat()
    overdue = hyp(eng, "Webinars convert better than ebooks.", "grok-good",
                  review_by=past)
    pending = hyp(eng, "Partners will resell the agency plan.", "grok-good",
                  review_by=future)
    out = eng.prime(writer="claude-code")
    block = out.split("[awaiting resolution]")[1].split("\n[")[0]
    assert overdue.id in block and pending.id not in block
    eng.resolve(overdue.id, "confirmed", writer="owner")
    assert "[awaiting resolution]" not in eng.prime(writer="claude-code")
    eng.close()


def test_brier_math():
    assert reputation.brier(0.9, "confirmed") == pytest.approx(0.01)
    assert reputation.brier(0.9, "refuted") == pytest.approx(0.81)
    assert reputation.brier(0.5, "mixed") == 0
    assert reputation.Reputation("x").reputation == pytest.approx(reputation.NEUTRAL)
    assert reputation.multiplier(5.0) == reputation.MULT_MAX
    assert reputation.multiplier(-5.0) == reputation.MULT_MIN
