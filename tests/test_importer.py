"""Importing an existing decision log / state file into memory."""

import pytest

from mnemosyne.config import Config, WriterSpec
from mnemosyne.engine import MemoryEngine
from mnemosyne.importers.decision_log import coverage, import_log, parse

LOG = """# DECISION LOG
Format: date | decision | charter ref | rationale | tier | approval

2026-07-10 | Charter adopted: the business plan is source of truth | §0 | Owner directive | — | Owner
2026-07-14 | Daily agent spend cap $75 → $50 until first revenue | Guidelines §3 | conserve burn | Tier 3 | Owner-authorized

## 2026-07-23 — REQ-20260723-01 APPROVED: journey spend
Owner approved up to $40 for PR #45 validation runs.
- run budget enforced by the harness

- **Owner rulings (2026-10-16, later):**
  - Phase 1 scope: receptionist inbound only; **no outbound SMS**.
  - Invoice reminders in approve-to-send mode.
- **Live Stripe and production remain Tier 2.**
- **Owner ruling (2026-10-19):** niche AI agent systems on our own platform.
"""

STATE = """# STATE — Build Director
Guidelines version acknowledged: v1.2

## 🔨 2026-10-20: LINEUP LIVE ON MAIN (#129 merged)
- Merged the lineup.

## Current phase
Charter v2 Phase 1 — Landing + Audit.
"""


def make_engine(tmp) -> MemoryEngine:
    cfg = Config(root=tmp, provider="dev", writers={"owner": WriterSpec(3),
                                                     "director": WriterSpec(2)})
    cfg.ensure_dirs()
    return MemoryEngine(cfg)


def test_parses_all_three_entry_shapes():
    es = parse(LOG)
    assert [e.date for e in es] == ["2026-07-10", "2026-07-14", "2026-07-23",
                                    "2026-10-16", "2026-10-19"]
    assert es[0].title.startswith("Charter adopted") and "Owner directive" in es[0].body
    assert es[2].title.startswith("2026-07-23 — REQ-20260723-01") and "$40" in es[2].body
    # an undated bullet after a dated ruling stays with that ruling
    assert "no outbound SMS" in es[3].raw and "remain Tier 2" in es[3].raw
    assert coverage(LOG) == pytest.approx(10 / 12)  # all but the title + format line


def test_state_files_keep_undated_current_sections():
    es = parse(STATE, undated="2026-10-25")
    assert [(e.date, e.title.split(":")[0][-10:]) for e in es][-1] == ("2026-10-25", "rent phase")
    assert "Landing + Audit" in es[-1].body
    assert len(parse(STATE)) == 1, "without a date for undated sections they are skipped"


def test_import_is_faithful_idempotent_and_screened(tmp_path):
    eng = make_engine(tmp_path)
    log = LOG + "\n2026-10-24 | Rotated the key sk_live_51HxyzABCDEFGHIJKLMNOP | §2 | leak | Tier 3 | Owner\n"
    report = import_log(eng, log, kind="decision", writer="director", source="import:LOG")
    assert len(report.imported) == 5 and len(report.skipped) == 1
    assert "secret" in report.skipped[0]["reason"].lower() or "contains" in report.skipped[0]["reason"]
    mems = list(eng.store.iter_memories())
    assert all(m.type.value == "decision" and m.ring == "canon" and m.writer == "director"
               for m in mems)
    sms = next(m for m in mems if "outbound SMS" in m.text)
    assert sms.valid_from == "2026-10-16" and "imported" in sms.tags
    # the verbatim entry is in the archive
    assert "remain Tier 2" in eng.read_archive(sms)
    # PR / REQ references become entities for graph recall
    req = next(m for m in mems if m.valid_from == "2026-07-23")
    assert {"REQ-20260723-01", "PR #45"} <= set(req.entities)
    hits = eng.recall("did we decide to send outbound SMS?", writer="director")
    assert hits and "outbound SMS" in hits[0].memory.text

    again = import_log(eng, log, kind="decision", writer="director")
    assert again.imported == [] and again.already_present == 5
    assert eng.store.count() == 5
    eng.close()


def test_dry_run_writes_nothing(tmp_path):
    eng = make_engine(tmp_path)
    report = import_log(eng, LOG, dry_run=True)
    assert len(report.imported) == 5 and eng.store.count() == 0
    with pytest.raises(ValueError):
        import_log(eng, LOG, kind="poem")
    eng.close()


def test_eval_harness_scores_recall(tmp_path):
    from mnemosyne.evaluate import evaluate
    eng = make_engine(tmp_path)
    import_log(eng, LOG, writer="director")
    result = evaluate(eng, [
        {"q": "Are we allowed outbound SMS in phase one?", "expect": "no outbound SMS"},
        {"q": "Which charter is the source of truth?", "expect": "Charter adopted"},
        {"q": "What colour is the office cat?", "expect": "a memory that does not exist"},
    ], k=3, writer="director")
    assert result["questions"] == 3 and result["hit@3"] == pytest.approx(2 / 3, abs=1e-3)
    assert result["hit@1"] <= result["hit@3"] and 0 < result["mrr"] <= result["hit@3"]
    assert [m["q"] for m in result["misses"]] == ["What colour is the office cat?"]
    eng.close()
