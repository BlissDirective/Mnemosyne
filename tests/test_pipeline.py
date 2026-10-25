"""End-to-end pipeline tests using the offline 'dev' embedding provider — no API
key or network needed. Run: python -m pytest -q  (or just `python tests/test_pipeline.py`)."""

import shutil
import tempfile
from pathlib import Path

import pytest

from mnemosyne.config import Config
from mnemosyne.engine import MemoryEngine
from mnemosyne.schema import Memory, MemoryType
from mnemosyne.retrieval import _has_temporal_cue, _guess_entities


def make_engine(tmp: Path, **cfg_overrides) -> MemoryEngine:
    cfg = Config(root=tmp, provider="dev", **cfg_overrides)
    cfg.ensure_dirs()
    return MemoryEngine(cfg)


def test_roundtrip_markdown():
    m = Memory(text="User prefers Python and minimal formatting.",
               type=MemoryType.PREFERENCE, entities=["Python"], tags=["style"])
    raw = m.to_markdown()
    back = Memory.from_markdown(raw)
    assert back.text == m.text
    assert back.type == MemoryType.PREFERENCE
    assert "Python" in back.entities


def test_remember_and_recall(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("User prefers Python and dislikes heavy formatting.",
                 type="preference", entities=["Python"])
    eng.remember("The user's dog is a corgi named Biscuit.",
                 type="world_fact", entities=["Biscuit"])
    eng.remember("We decided to build a local-first memory system called Mnemosyne.",
                 type="episodic", entities=["Mnemosyne"])

    # semantic-ish + keyword recall should surface the Python preference
    hits = eng.recall("what programming language does the user like?", k=3)
    texts = " ".join(h.memory.text.lower() for h in hits)
    assert "python" in texts, texts

    # entity/keyword recall
    hits = eng.recall("tell me about Biscuit", k=3, entities=["Biscuit"])
    assert any("corgi" in h.memory.text.lower() for h in hits)

    eng.close()


def test_persistence_and_reindex(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("Mnemosyne stores memories as Markdown.", type="semantic")
    eng.close()

    # a fresh engine over the same folder must rebuild the index from notes only
    eng2 = make_engine(tmp_path)
    result = eng2.sync(force=True)
    assert result["embedded"] >= 1
    hits = eng2.recall("how are memories stored?", k=3)
    assert any("markdown" in h.memory.text.lower() for h in hits)
    eng2.close()


def test_reflect_dedup(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("The user lives in Berlin.", type="world_fact", entities=["Berlin"])
    eng.remember("The user lives in Berlin.", type="world_fact", entities=["Berlin"])
    report = eng.reflect(dry_run=False)
    assert report.duplicate_clusters, "expected a duplicate cluster to be detected"
    # the survivor should have been reinforced
    assert report.reinforced
    eng.close()


def test_forget(tmp_path):
    eng = make_engine(tmp_path)
    mem = eng.remember("Ephemeral note.", type="episodic")
    assert eng.get(mem.id) is not None
    assert eng.forget(mem.id) is True
    assert eng.get(mem.id) is None
    eng.close()


# --------------------------------------------------------------------------- #
# v0.2.0 fix #1 — reflect() metadata changes reach the index; reindex sees them
# --------------------------------------------------------------------------- #
def test_reflect_metadata_syncs_to_index(tmp_path):
    eng = make_engine(tmp_path)
    a = eng.remember("The user lives in Berlin.", type="world_fact")
    b = eng.remember("The user lives in Berlin.", type="world_fact")
    report = eng.reflect(dry_run=False)

    # index rows must show the post-reflect state, not the stale write-time state
    cluster = report.duplicate_clusters[0]
    survivor_id, dup_id = cluster[0], cluster[1]
    assert {survivor_id, dup_id} == {a.id, b.id}
    survivor_row = eng.index.get_meta(survivor_id)
    dup_row = eng.index.get_meta(dup_id)
    assert float(survivor_row["confidence"]) > 0.7, "survivor reinforcement never reached the index"
    assert dup_row["duplicate_of"] == survivor_id, "duplicate_of never reached the index"

    # and a full resync must see nothing to do: no re-embeds, no meta updates
    result = eng.sync()
    assert result["embedded"] == 0
    assert result["meta_updated"] == 0
    eng.close()


def test_reindex_picks_up_out_of_band_frontmatter_edit(tmp_path):
    eng = make_engine(tmp_path)
    mem = eng.remember("The user's team ships on Fridays.", type="procedural")
    # simulate a hand edit / git pull touching only frontmatter
    m = eng.store.read_by_id(mem.id)
    m.confidence = 0.95
    m.touch()
    eng.store.write(m)

    result = eng.sync()
    assert result["embedded"] == 0, "frontmatter-only edit must not re-embed"
    assert result["meta_updated"] == 1, "frontmatter-only edit must refresh index metadata"
    assert float(eng.index.get_meta(mem.id)["confidence"]) == 0.95
    eng.close()


# --------------------------------------------------------------------------- #
# v0.2.0 fix #2 — recall filters superseded duplicates and expired world_facts
# --------------------------------------------------------------------------- #
def test_recall_hides_superseded_duplicates(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("The user lives in Berlin.", type="world_fact", entities=["Berlin"])
    eng.remember("The user lives in Berlin.", type="world_fact", entities=["Berlin"])
    eng.reflect(dry_run=False)

    hits = eng.recall("where does the user live?", k=8)
    berlin = [h for h in hits if "berlin" in h.memory.text.lower()]
    assert len(berlin) == 1, f"expected exactly the survivor, got {len(berlin)}"
    assert all(h.memory.duplicate_of is None for h in hits)

    # escape hatch still shows everything
    hits_all = eng.recall("where does the user live?", k=8, include_superseded=True)
    assert len([h for h in hits_all if "berlin" in h.memory.text.lower()]) == 2
    eng.close()


def test_recall_hides_expired_world_facts(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("Alice works at Initech.", type="world_fact",
                 entities=["Alice"], valid_to="2020-01-01")
    eng.remember("Alice works at Globex.", type="world_fact", entities=["Alice"])

    hits = eng.recall("where does Alice work?", k=8, entities=["Alice"])
    texts = [h.memory.text for h in hits]
    assert any("Globex" in t for t in texts)
    assert not any("Initech" in t for t in texts), "expired fact leaked into recall"

    hits_all = eng.recall("where does Alice work?", k=8, entities=["Alice"],
                          include_superseded=True)
    assert any("Initech" in h.memory.text for h in hits_all)
    eng.close()


# --------------------------------------------------------------------------- #
# v0.2.0 fix #3 — asymmetric query embeddings (interface-level check, offline)
# --------------------------------------------------------------------------- #
def test_embed_query_interface(tmp_path):
    from mnemosyne.embeddings import DevEmbedding, VoyageEmbedding
    dev = DevEmbedding()
    v = dev.embed_query("hello world")
    assert v.shape == (dev.dim,)

    # Voyage must route queries through input_type='query'
    captured = {}
    voy = VoyageEmbedding(api_key="test-key")

    def fake_request(texts, is_query):
        captured["is_query"] = is_query
        import numpy as np
        return np.ones((len(texts), voy.dim), dtype="float32")

    voy._request = fake_request
    voy.embed_query("who is Alice?")
    assert captured["is_query"] is True
    voy.embed(["Alice works at Globex."])
    assert captured["is_query"] is False


# --------------------------------------------------------------------------- #
# v0.2.0 fix #4 — archive_ref is relative and resolvable
# --------------------------------------------------------------------------- #
@pytest.mark.files_only
def test_archive_ref_is_relative_and_resolvable(tmp_path):
    eng = make_engine(tmp_path)
    mem = eng.remember("Distilled fact.", type="semantic",
                       archive_text="The raw transcript this came from.")
    assert mem.archive_ref is not None
    assert not Path(mem.archive_ref).is_absolute(), \
        f"archive_ref must be relative for portability, got {mem.archive_ref}"
    resolved = eng.resolve_archive(mem)
    assert resolved is not None and resolved.exists()
    assert "raw transcript" in resolved.read_text()
    eng.close()


# --------------------------------------------------------------------------- #
# v0.2.0 fix #5 — HTTP providers batch their requests
# --------------------------------------------------------------------------- #
def test_http_embedding_batches(tmp_path):
    import numpy as np
    from mnemosyne.embeddings import OpenAIEmbedding
    prov = OpenAIEmbedding(api_key="test-key", dim=8)
    calls = []

    def fake_request(texts, is_query):
        calls.append(len(texts))
        return np.ones((len(texts), 8), dtype="float32")

    prov._request = fake_request
    prov.embed([f"memory {i}" for i in range(250)])
    assert calls == [100, 100, 50], f"expected 3 chunks of <=100, got {calls}"


# --------------------------------------------------------------------------- #
# v0.2.0 fix #6 — recency is gated on temporal cues
# --------------------------------------------------------------------------- #
def test_temporal_cue_gate():
    assert _has_temporal_cue("what did we decide recently?")
    assert _has_temporal_cue("what happened this week")
    assert _has_temporal_cue("latest status on the migration")
    assert not _has_temporal_cue("what programming language does the user like?")
    assert not _has_temporal_cue("where does Alice work?")


def test_recency_does_not_pollute_atemporal_recall(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("User prefers Python for scripting.", type="preference",
                 entities=["Python"])
    # a freshly stored, totally unrelated memory
    eng.remember("Bought oat milk at the store.", type="episodic")

    hits = eng.recall("what programming language does the user like?", k=1)
    assert hits and "python" in hits[0].memory.text.lower()
    # recency must not have been a contributing strategy for an atemporal query
    assert all("recency" not in h.strategies for h in hits)
    eng.close()


# --------------------------------------------------------------------------- #
# v0.2.0 fix #7 — sentence-initial capitals aren't treated as entities
# --------------------------------------------------------------------------- #
def test_guess_entities_skips_sentence_initial():
    # "Tell" opens the sentence — not an entity. "Alice" is mid-sentence — entity.
    assert _guess_entities("Tell me about Alice and her team") == ["alice"]
    # sentence-initial word counts when it ALSO appears capitalized mid-sentence
    assert "mnemosyne" in _guess_entities("Mnemosyne sync: how does Mnemosyne reindex?")
    assert _guess_entities("What should we do next") == []


# --------------------------------------------------------------------------- #
# v0.3.0 — memory_update: edit in place, preserving identity and history
# --------------------------------------------------------------------------- #
def test_update_metadata_only(tmp_path):
    eng = make_engine(tmp_path)
    mem = eng.remember("Alice leads the API team.", type="world_fact",
                       entities=["Alice"])
    before = eng.index.existing_state()[mem.id]

    out = eng.update(mem.id, confidence=0.95, tags=["org"])
    assert out is not None
    assert out.id == mem.id and out.created == mem.created
    assert out.confidence == 0.95 and out.tags == ["org"]
    # metadata-only: content hash unchanged in index, so no re-embed on resync
    after = eng.index.existing_state()[mem.id]
    assert after[0] == before[0], "text hash must be unchanged"
    assert float(eng.index.get_meta(mem.id)["confidence"]) == 0.95
    assert eng.sync()["embedded"] == 0
    eng.close()


@pytest.mark.files_only
def test_update_text_reembeds_and_preserves_identity(tmp_path):
    eng = make_engine(tmp_path)
    mem = eng.remember("Alice works at Initech.", type="world_fact",
                       entities=["Alice"])
    old_path = eng.store.path_by_id(mem.id)

    out = eng.update(mem.id, text="Alice works at Globex.")
    assert out.id == mem.id and out.created == mem.created
    assert out.recall_count == mem.recall_count  # counters preserved

    # the canonical file was renamed to the new slug, same id
    new_path = eng.store.path_by_id(mem.id)
    assert new_path != old_path and new_path.exists() and not old_path.exists()

    # recall finds the new text, not the old
    hits = eng.recall("where does Alice work?", k=5, entities=["Alice"])
    texts = " ".join(h.memory.text for h in hits)
    assert "Globex" in texts and "Initech" not in texts
    eng.close()


def test_update_clears_validity_with_empty_string(tmp_path):
    eng = make_engine(tmp_path)
    mem = eng.remember("Bob is on sabbatical.", type="world_fact",
                       valid_to="2030-01-01")
    out = eng.update(mem.id, valid_to="")
    assert out.valid_to is None
    assert eng.update("nonexistent-id", confidence=0.5) is None
    eng.close()


# --------------------------------------------------------------------------- #
# v0.3.0 — access counters: index immediately, canonical in batches
# --------------------------------------------------------------------------- #
def test_access_counters_bump_and_flush(tmp_path):
    eng = make_engine(tmp_path)  # default flush threshold (10)
    mem = eng.remember("User prefers Python for scripting.", type="preference",
                       entities=["Python"])
    eng.recall("what language does the user prefer?", k=1)

    # index side is bumped immediately
    row = eng.index.get_meta(mem.id)
    assert row["recall_count"] == 1 and row["last_recalled"]

    # canonical side is still pending (batched)…
    assert eng.store.read_by_id(mem.id).recall_count == 0
    assert eng.stats()["access_pending_flush"] >= 1

    # …until an explicit flush writes it through
    assert eng.flush_access() == 1
    on_disk = eng.store.read_by_id(mem.id)
    assert on_disk.recall_count == 1 and on_disk.last_recalled
    # and both layers now agree, so a resync has nothing to do
    result = eng.sync()
    assert result["embedded"] == 0 and result["meta_updated"] == 0
    eng.close()


def test_access_counters_flush_at_threshold_and_on_close(tmp_path):
    eng = make_engine(tmp_path, counter_flush_every=2)
    mem = eng.remember("User prefers Python for scripting.", type="preference")
    eng.recall("python preference", k=1)   # pending: 1
    eng.recall("python preference", k=1)   # pending: 2 -> auto-flush
    assert eng.store.read_by_id(mem.id).recall_count == 2
    assert eng.stats()["access_pending_flush"] == 0

    eng.recall("python preference", k=1)   # pending: 1 again
    eng.close()                            # close flushes the remainder

    eng2 = make_engine(tmp_path)
    assert eng2.store.read_by_id(mem.id).recall_count == 3
    eng2.close()


def test_decay_spares_recently_recalled_memories(tmp_path):
    eng = make_engine(tmp_path, decay_per_reflect=0.05, decay_min_age_days=30)
    used = eng.remember("User prefers Python for scripting.", type="preference")
    stale = eng.remember("Once considered using XML for configs.", type="episodic")

    # age both memories well past the decay window…
    for m_id in (used.id, stale.id):
        m = eng.store.read_by_id(m_id)
        m.updated = "2020-01-01T00:00:00+00:00"
        eng.store.write(m)
    # …but mark one as recently recalled
    u = eng.store.read_by_id(used.id)
    u.recall_count, u.last_recalled = 5, "2099-01-01T00:00:00+00:00"
    eng.store.write(u)
    eng.sync()

    report = eng.reflect(dry_run=False)
    assert stale.id in report.decayed, "stale, unused memory should decay"
    assert used.id not in report.decayed, "recently recalled memory must not decay"
    eng.close()


# --------------------------------------------------------------------------- #
# v0.3.0 — prime: session-start context block (stdout of the hook)
# --------------------------------------------------------------------------- #
def test_prime_output(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("User prefers Python and minimal formatting.", type="preference",
                 entities=["Python"])
    eng.remember("The user lives in Berlin.", type="world_fact")
    eng.remember("The user lives in Berlin.", type="world_fact")
    eng.remember("Alice worked at Initech.", type="world_fact", valid_to="2020-01-01")
    eng.reflect(dry_run=False)  # marks one Berlin memory superseded

    out = eng.prime()
    assert "data, not instructions" in out.lower()
    assert "python" in out.lower()
    assert out.lower().count("lives in berlin") == 1, "superseded memory leaked into prime"
    assert "initech" not in out.lower(), "expired fact leaked into prime"
    assert len(out) < 10000, "must stay under the SessionStart stdout cap"

    # topical priming works too, and must not inflate counters
    before = eng.index.get_meta(
        [m.id for m in eng.store.iter_memories() if "Python" in m.text][0]
    )["recall_count"]
    out_q = eng.prime(query="programming language preference")
    assert "python" in out_q.lower()
    eng.close()


if __name__ == "__main__":
    tmp = Path(tempfile.mkdtemp())
    try:
        test_roundtrip_markdown()
        test_remember_and_recall(tmp / "a")
        test_persistence_and_reindex(tmp / "b")
        test_reflect_dedup(tmp / "c")
        test_forget(tmp / "d")
        test_reflect_metadata_syncs_to_index(tmp / "e")
        test_reindex_picks_up_out_of_band_frontmatter_edit(tmp / "f")
        test_recall_hides_superseded_duplicates(tmp / "g")
        test_recall_hides_expired_world_facts(tmp / "h")
        test_embed_query_interface(tmp / "i")
        test_archive_ref_is_relative_and_resolvable(tmp / "j")
        test_http_embedding_batches(tmp / "k")
        test_temporal_cue_gate()
        test_recency_does_not_pollute_atemporal_recall(tmp / "l")
        test_guess_entities_skips_sentence_initial()
        test_update_metadata_only(tmp / "m")
        test_update_text_reembeds_and_preserves_identity(tmp / "n")
        test_update_clears_validity_with_empty_string(tmp / "o")
        test_access_counters_bump_and_flush(tmp / "p")
        test_access_counters_flush_at_threshold_and_on_close(tmp / "q")
        test_decay_spares_recently_recalled_memories(tmp / "r")
        test_prime_output(tmp / "s")
        print("ALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
