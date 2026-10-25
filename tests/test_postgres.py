"""The Postgres backend (storage = "postgres"): the guarantees the file
backend's layout tests prove, re-proven for rows — plus tenancy and
cross-process concurrency, which only matter here.

Runs whenever MNEMOSYNE_TEST_DATABASE_URL points at a Postgres with pgvector:

    MNEMOSYNE_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1/mnemosyne_test \\
        python -m pytest -q tests/test_postgres.py
"""

import json
import os
import threading
import uuid
from pathlib import Path

import pytest

from mnemosyne import journal as journal_mod
from mnemosyne.config import Config, WriterSpec
from mnemosyne.engine import MemoryEngine

URL = os.environ.get("MNEMOSYNE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs MNEMOSYNE_TEST_DATABASE_URL")

ROSTER = {"owner": WriterSpec(3), "director": WriterSpec(2, teams=["ops"]),
          "librarian": WriterSpec(2), "grok-operator": WriterSpec(1, teams=["ops"])}


@pytest.fixture(scope="module")
def schema():
    name = f"mnemo_pgtest_{uuid.uuid4().hex[:10]}"
    yield name
    import psycopg
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


def pg_engine(tmp, schema, workspace=None, **kw) -> MemoryEngine:
    cfg = Config(root=Path(tmp), provider="dev", writers=dict(ROSTER), storage="postgres",
                 database_url=URL, pg_schema=schema,
                 workspace=workspace or f"ws_{uuid.uuid4().hex[:8]}", **kw)
    cfg.ensure_dirs()
    return MemoryEngine(cfg)


def files_engine(root) -> MemoryEngine:
    cfg = Config(root=Path(root), provider="dev", writers=dict(ROSTER), storage="files")
    return MemoryEngine(cfg)


def busy(eng):
    a = eng.remember("Acme pays $500 per month.", type="world_fact", entities=["Acme"],
                     ring="canon", writer="owner", archive_text="call notes: Acme said 500")
    eng.remember("Acme pays $500 per month.", type="world_fact", entities=["Acme"],
                 ring="canon", writer="director")
    q = eng.remember("Initech wants SSO.", ring="canon", writer="grok-operator")
    eng.promote(q.id, writer="director")
    h = eng.remember("Annual billing lifts retention.", type="hypothesis", ring="canon",
                     writer="director")
    eng.resolve(h.id, "confirmed", writer="owner")
    eng.relate(q.id, "supports", a.id, writer="director")
    eng.recall("Acme")
    eng.reflect(writer="librarian")
    junk = eng.remember("Temporary.", writer="owner")
    eng.forget(junk.id, writer="owner")
    eng.flush_access()
    return a, q, h


def state(eng):
    return {m.id: m.to_json() for m in eng.store.iter_memories()}


# --------------------------------------------------------------------------- #
# walk-away guarantee: export = a working, verifiable file-backend store
# --------------------------------------------------------------------------- #
def test_export_is_an_exact_verifiable_file_store(tmp_path, schema):
    eng = pg_engine(tmp_path / "pg", schema)
    a, q, h = busy(eng)
    out = eng.export(tmp_path / "export")
    assert out["memories"] == eng.store.count() and out["journal_shards"]

    fe = files_engine(tmp_path / "export")
    v = fe.verify_journal()
    assert v["ok"] and v["events"] == eng.verify_journal()["events"]
    assert v["head"] == eng.verify_journal()["head"], "same events, same hash chain"
    assert state(fe) == state(eng), "exported notes == canonical rows"
    assert fe.sync()["journaled_external"] == 0, "export matches its own journal exactly"
    assert "Acme pays $500 per month." in {x.memory.text for x in fe.recall("Acme")}
    # reputation pages and the verbatim archive come along
    assert (tmp_path / "export/notes/canon/_writers/director.md").exists()
    archived = list((tmp_path / "export/archive").glob("*.md"))
    assert archived and "Acme said 500" in archived[0].read_text()
    eng.close()
    fe.close()


def test_journal_rows_are_tamper_evident(tmp_path, schema):
    eng = pg_engine(tmp_path, schema)
    busy(eng)
    assert eng.verify_journal()["ok"]
    db, ws = eng.store.db, eng.cfg.workspace
    row = db.q("SELECT seq, event FROM {s}.journal WHERE workspace=%s ORDER BY seq "
               "OFFSET 2 LIMIT 1", (ws,), fetch="one")
    ev = row["event"]
    ev["state"]["text"] = "Acme pays $0 per month."
    db.q("UPDATE {s}.journal SET event=%s WHERE workspace=%s AND seq=%s",
         (json.dumps(ev), ws, row["seq"]), fetch="none")
    bad = eng.verify_journal()
    assert not bad["ok"] and f"seq {row['seq']}" in bad["error"] and "edited" in bad["error"]
    # a deleted row is caught by the next event's prev_hash
    db.q("DELETE FROM {s}.journal WHERE workspace=%s AND seq=%s", (ws, row["seq"]),
         fetch="none")
    assert "removed" in eng.verify_journal()["error"]
    eng.close()


# --------------------------------------------------------------------------- #
# canonical / derived split
# --------------------------------------------------------------------------- #
def test_index_rebuilds_from_canonical_rows(tmp_path, schema):
    eng = pg_engine(tmp_path, schema)
    a, q, h = busy(eng)
    before_reps = eng.reputations()
    before_hits = [x.memory.id for x in eng.recall("Acme price", entities=["Acme"])]
    db, ws = eng.store.db, eng.cfg.workspace
    for tbl in ("idx", "idx_entities", "idx_relations", "idx_reputation"):
        db.q(f"DELETE FROM {{s}}.{tbl} WHERE workspace=%s", (ws,), fetch="none")
    assert eng.recall("Acme price", entities=["Acme"]) == []
    result = eng.sync()
    assert result["embedded"] == eng.store.count()
    assert eng.reputations() == before_reps
    assert [x.memory.id for x in eng.recall("Acme price", entities=["Acme"])] == before_hits
    assert {(s, r, d) for s, r, d, _ in eng.index.relation_edges()} >= {(q.id, "supports", a.id)}
    eng.close()


def test_out_of_band_row_edit_is_journaled(tmp_path, schema):
    eng = pg_engine(tmp_path, schema)
    a, q, h = busy(eng)
    db, ws = eng.store.db, eng.cfg.workspace
    doc = db.q("SELECT doc FROM {s}.memories WHERE workspace=%s AND id=%s", (ws, q.id),
               fetch="one")["doc"]
    doc["text"] = "Initech wants SSO and SCIM."
    db.q("UPDATE {s}.memories SET doc=%s WHERE workspace=%s AND id=%s",
         (json.dumps(doc), ws, q.id), fetch="none")
    result = eng.sync()
    assert result["journaled_external"] == 1 and result["embedded"] == 1
    assert list(eng.journal.events())[-1]["op"] == "external"
    assert eng.sync()["journaled_external"] == 0
    assert "SCIM" in eng.recall("Initech SCIM")[0].memory.text
    eng.close()


# --------------------------------------------------------------------------- #
# tenancy and concurrency
# --------------------------------------------------------------------------- #
def test_workspaces_are_isolated(tmp_path, schema):
    a = pg_engine(tmp_path / "a", schema, workspace="client_a")
    b = pg_engine(tmp_path / "b", schema, workspace="client_b")
    a.remember("Client A's customer Dana prefers SMS.", ring="canon", writer="owner",
               entities=["Dana"])
    b.remember("Client B's customer Dana prefers email.", ring="canon", writer="owner",
               entities=["Dana"])
    assert [h.memory.text for h in a.recall("Dana", entities=["Dana"])] == \
        ["Client A's customer Dana prefers SMS."]
    assert [h.memory.text for h in b.recall("Dana", entities=["Dana"])] == \
        ["Client B's customer Dana prefers email."]
    assert a.verify_journal()["events"] == 1 and b.verify_journal()["events"] == 1
    a.close()
    b.close()


def test_two_processes_on_one_workspace_stay_linear(tmp_path, schema):
    """Two engines with their own connections stand in for two server
    replicas: the advisory lock keeps writes and the hash chain linear."""
    ws = f"ws_{uuid.uuid4().hex[:8]}"
    engines = [pg_engine(tmp_path / f"r{i}", schema, workspace=ws, counter_flush_every=2)
               for i in range(2)]
    errors, ids = [], []

    def worker(n):
        eng = engines[n % 2]
        try:
            for i in range(15):
                m = eng.remember(f"Replica {n} fact {i} about topic {i % 4}.", ring="canon",
                                 writer="director", entities=[f"t{i % 4}"])
                ids.append(m.id)
                eng.recall(f"topic {i % 4}", k=3, writer="director")
                if i % 3 == 0:
                    eng.update(m.id, confidence=0.8, writer="director")
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    for e in engines:
        e.flush_access()
    eng = engines[0]
    assert len(ids) == len(set(ids)) == 90 and eng.store.count() == 90
    v = eng.verify_journal()
    assert v["ok"], v["error"]
    replayed = journal_mod.replay_states(eng.journal.events())
    assert {k: m.to_json() for k, m in replayed.items()} == state(eng)
    sync = eng.sync()
    assert sync["embedded"] == 0 and sync["meta_updated"] == 0
    for e in engines:
        e.close()


def test_replicas_do_not_lose_updates_to_a_shared_memory(tmp_path, schema):
    """Read-modify-write of ONE memory from two replicas at once (access
    counters flushed on every recall): without the per-workspace advisory
    lock, increments are lost."""
    ws = f"ws_{uuid.uuid4().hex[:8]}"
    engines = [pg_engine(tmp_path / f"r{i}", schema, workspace=ws, counter_flush_every=1)
               for i in range(2)]
    m = engines[0].remember("Shared fact everyone recalls.", ring="canon", writer="owner",
                            entities=["shared"])
    errors = []

    def worker(n):
        try:
            for _ in range(20):
                engines[n % 2].recall("shared fact", entities=["shared"], k=1,
                                      writer="director")
        except Exception as e:  # pragma: no cover
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    assert engines[0].get(m.id).recall_count == 120, "lost updates across replicas"
    assert engines[0].verify_journal()["ok"]
    for e in engines:
        e.close()


# --------------------------------------------------------------------------- #
# search paths
# --------------------------------------------------------------------------- #
def test_hnsw_path_and_exact_path_agree(tmp_path, schema):
    exact = pg_engine(tmp_path / "x", schema, workspace="ann_check")
    for i in range(40):
        exact.remember(f"Note {i}: customer {i % 7} asked about invoice timing.",
                       ring="canon", writer="owner", entities=[f"c{i % 7}"])
    target = exact.remember("Globex asked to move their renewal to March.", ring="canon",
                            writer="owner", entities=["Globex"])
    ann = pg_engine(tmp_path / "y", schema, workspace="ann_check", exact_search_max=0)
    q = "When is the Globex renewal?"
    assert exact.recall(q, k=3)[0].memory.id == target.id
    assert ann.recall(q, k=3)[0].memory.id == target.id
    exact.close()
    ann.close()


def test_embedding_model_swap_is_a_reindex(tmp_path, schema):
    """Evolutionary property on Postgres: a new model (here, a new dimension)
    is `reindex --force`, never a migration; both dimensions can coexist."""
    eng = pg_engine(tmp_path, schema, workspace="swap")
    eng.remember("Initech's buyer is Dana Lee.", ring="canon", writer="owner",
                 entities=["Initech"])
    eng.close()
    eng2 = pg_engine(tmp_path, schema, workspace="swap", dim=128)
    assert eng2.sync(force=True)["embedded"] == 1
    assert eng2.stats()["by_embed_model"] == {"dev-hash-128": 1}
    assert eng2.recall("Who buys at Initech?")[0].memory.text.startswith("Initech")
    eng2.close()


def test_as_of_and_archive_on_postgres(tmp_path, schema, monkeypatch):
    clock = iter(["2026-09-01T09:00:00+00:00", "2026-10-05T09:00:00+00:00",
                  "2026-10-06T09:00:00+00:00", "2026-10-07T09:00:00+00:00"])
    monkeypatch.setattr(journal_mod, "utcnow_iso", lambda: next(clock))
    eng = pg_engine(tmp_path, schema)
    m = eng.remember("Acme pays $500 per month.", ring="canon", writer="owner",
                     entities=["Acme"], archive_text="raw call transcript")
    eng.update(m.id, text="Acme pays $900 per month.", writer="owner")
    then = eng.recall("Acme price", entities=["Acme"], as_of="2026-09-10")
    assert [h.memory.text for h in then] == ["Acme pays $500 per month."]
    assert "raw call transcript" in eng.store.read_archive(m.archive_ref)
    eng.close()


def test_database_url_never_in_config_file(tmp_path):
    from mnemosyne.config import load_config
    (tmp_path / "config.toml").write_text(
        '[storage]\nbackend = "postgres"\ndatabase_url = "postgresql://u:pw@h/db"\n')
    with pytest.raises(ValueError, match="must not be in config.toml"):
        load_config(str(tmp_path))
