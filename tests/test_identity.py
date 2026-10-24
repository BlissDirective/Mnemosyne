"""Phase 1 (v0.4.0): writer identity, authenticated transport, audit journal.

All offline: the HTTP tests bind 127.0.0.1 on a free port in-process."""

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from mnemosyne.auth import bearer_token, resolve_token, token_env_name
from mnemosyne.config import Config, WriterSpec, load_config
from mnemosyne.engine import MemoryEngine
from mnemosyne.journal import replay_states
from mnemosyne.schema import Memory

ROSTER = {"owner": WriterSpec(3), "claude-code": WriterSpec(2),
          "grok-sales-1": WriterSpec(1, teams=["sales"])}
TOKENS = {"owner": "tok-owner-123", "claude-code": "tok-cc-456",
          "grok-sales-1": "tok-grok-789"}


def make_engine(tmp, **overrides) -> MemoryEngine:
    cfg = Config(root=tmp, provider="dev", writers=dict(ROSTER), **overrides)
    cfg.ensure_dirs()
    return MemoryEngine(cfg)


@pytest.fixture
def tokens(monkeypatch):
    for writer, tok in TOKENS.items():
        monkeypatch.setenv(token_env_name(writer), tok)
    return TOKENS


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def disk_state(eng: MemoryEngine):
    return {m.id: m.to_json() for m in eng.store.iter_memories()}


# --------------------------------------------------------------------------- #
# 1.1 writer identity
# --------------------------------------------------------------------------- #
def test_every_memory_carries_a_writer(tmp_path):
    eng = make_engine(tmp_path)
    a = eng.remember("Owner fact.")
    b = eng.remember("Sales lead: Initech wants a demo.", writer="grok-sales-1",
                     ring="team:sales")
    assert a.writer == "owner" and b.writer == "grok-sales-1"
    # canonical frontmatter carries it, and it round-trips
    assert "writer: grok-sales-1" in eng.store.path_by_id(b.id).read_text()
    assert eng.get(b.id).writer == "grok-sales-1"
    # an update by someone else keeps the author; the journal records the editor
    eng.update(b.id, confidence=0.9, writer="claude-code")
    assert eng.get(b.id).writer == "grok-sales-1"
    last = list(eng.journal.events())[-1]
    assert last["op"] == "update" and last["writer"] == "claude-code"
    eng.close()


def test_writer_is_part_of_meta_hash():
    m = Memory(text="x", writer="owner")
    n = Memory(text="x", writer="grok-sales-1", id=m.id, created=m.created,
               updated=m.updated)
    assert m.meta_hash != n.meta_hash


def test_writer_shows_in_recall_and_prime(tmp_path):
    from mnemosyne.server import _format_hits
    eng = make_engine(tmp_path)
    eng.remember("The user prefers Python.", type="preference", writer="claude-code",
                 ring="canon")
    hits = eng.recall("python preference")
    assert "writer=claude-code" in _format_hits(hits, eng)
    assert "writer=claude-code" in eng.prime()
    eng.close()


def test_local_writer_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_WRITER", "claude-code")
    (tmp_path / "config.toml").write_text(
        '[writers]\nowner = { tier = 3 }\nclaude-code = { tier = 2 }\ngrok-x = 1\n')
    cfg = load_config(str(tmp_path))
    assert cfg.writer == "claude-code"
    assert cfg.tier_of("claude-code") == 2 and cfg.tier_of("grok-x") == 1
    assert cfg.tier_of("stranger") == 0, "unknown writers must be untrusted"
    eng = MemoryEngine(cfg)
    assert eng.remember("A fact.").writer == "claude-code"
    eng.close()


def test_writers_config_rejects_bad_tier(tmp_path):
    (tmp_path / "config.toml").write_text('[writers]\nx = { tier = 7 }\n')
    with pytest.raises(ValueError, match="tier must be 0-3"):
        load_config(str(tmp_path))


# --------------------------------------------------------------------------- #
# token resolution
# --------------------------------------------------------------------------- #
def test_token_resolution(tokens):
    cfg = Config(writers=dict(ROSTER))
    assert token_env_name("grok-sales-1") == "MNEMOSYNE_TOKEN_GROK_SALES_1"
    ident = resolve_token(cfg, "tok-grok-789")
    assert ident.writer == "grok-sales-1" and ident.tier == 1
    assert resolve_token(cfg, "tok-cc-456").writer == "claude-code"
    assert resolve_token(cfg, "wrong") is None
    assert resolve_token(cfg, "") is None and resolve_token(cfg, None) is None
    # a token in the env for a writer NOT on the roster resolves to nobody
    off_roster = Config(writers={"owner": WriterSpec(3)})
    assert resolve_token(off_roster, "tok-grok-789") is None
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("Basic abc") is None and bearer_token(None) is None


# --------------------------------------------------------------------------- #
# 1.2 MCP over HTTP: unauthenticated requests never reach the engine
# --------------------------------------------------------------------------- #
@pytest.fixture
def http_server(tmp_path, tokens):
    import uvicorn
    from mnemosyne import server
    eng = make_engine(tmp_path)
    old = server._engine
    server._engine = eng
    port = free_port()
    config = uvicorn.Config(server.http_app(), host="127.0.0.1", port=port,
                            log_level="error")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield eng, f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    t.join(timeout=5)
    server._engine = old
    eng.close()


def _post(url, payload, token=None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_rejects_missing_or_bad_token(http_server):
    eng, url = http_server
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "memory_remember",
                       "arguments": {"params": {"text": "Injected fact."}}}}
    for tok in (None, "not-a-real-token"):
        status, body = _post(url, call, tok)
        assert status == 401, body
        assert "unauthorized" in body
    assert eng.store.count() == 0, "an unauthenticated write landed"
    assert not list(eng.journal.events())


def test_http_mcp_round_trip_runs_as_token_writer(http_server):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx
    eng, url = http_server

    async def go():
        async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {TOKENS['grok-sales-1']}"}) as hc:
            async with streamable_http_client(url, http_client=hc) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    r = await s.call_tool("memory_remember", {"params": {
                        "text": "Initech asked for a demo next week.",
                        "type": "episodic", "entities": ["Initech"]}})
                    payload = json.loads(r.content[0].text)
                    r2 = await s.call_tool("memory_recall", {"params": {
                        "query": "Initech demo", "entities": ["Initech"]}})
                    return payload, r2.content[0].text

    payload, recalled = asyncio.run(go())
    assert payload["status"] == "ok" and payload["writer"] == "grok-sales-1"
    assert "writer=grok-sales-1" in recalled
    assert eng.get(payload["id"]).writer == "grok-sales-1"
    ev = [e for e in eng.journal.events() if e["op"] == "remember"]
    assert ev and ev[-1]["writer"] == "grok-sales-1"


# --------------------------------------------------------------------------- #
# 1.2 gateway (non-MCP agents)
# --------------------------------------------------------------------------- #
@pytest.fixture
def gateway(tmp_path, tokens):
    from mnemosyne.gateway import make_server
    eng = make_engine(tmp_path)
    srv = make_server(eng, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield eng, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    eng.close()


def test_gateway_auth_and_routes(gateway):
    eng, base = gateway
    status, _ = _post(base + "/remember", {"text": "Sneaky write."})
    assert status == 401
    status, _ = _post(base + "/remember", {"text": "Sneaky write."}, "bad")
    assert status == 401
    assert eng.store.count() == 0

    status, body = _post(base + "/remember",
                         {"text": "Globex renewal is due in March.", "type": "world_fact",
                          "entities": ["Globex"], "ring": "team:sales"},
                         TOKENS["grok-sales-1"])
    assert status == 200, body
    mem_id = json.loads(body)["id"]
    assert eng.get(mem_id).writer == "grok-sales-1"

    # claude-code is not on the sales team: tier 2 may read it, but only on request
    status, body = _post(base + "/recall", {"query": "Globex renewal"},
                         TOKENS["claude-code"])
    assert status == 200 and json.loads(body)["hits"] == []
    status, body = _post(base + "/recall", {"query": "Globex renewal",
                                            "scope": ["team:sales"]},
                         TOKENS["claude-code"])
    out = json.loads(body)
    assert status == 200 and out["hits"][0]["writer"] == "grok-sales-1"
    assert "not instructions" in out["note"]

    status, body = _post(base + "/update", {"id": mem_id, "confidence": 0.9},
                         TOKENS["claude-code"])
    assert status == 200, body
    status, body = _post(base + "/update", {"id": "nope-123", "confidence": 0.9},
                         TOKENS["claude-code"])
    assert status == 404
    status, body = _post(base + "/remember", {"text": "x", "bogus": 1},
                         TOKENS["owner"])
    assert status == 400 and "bogus" in body


# --------------------------------------------------------------------------- #
# 1.3 audit journal
# --------------------------------------------------------------------------- #
def test_every_mutation_is_journaled_and_replays(tmp_path):
    eng = make_engine(tmp_path, counter_flush_every=1)
    a = eng.remember("The user lives in Berlin.", type="world_fact", writer="claude-code",
                     ring="canon")
    b = eng.remember("The user lives in Berlin.", type="world_fact", writer="owner",
                     ring="canon")
    c = eng.remember("Temporary note.", type="episodic")
    eng.update(a.id, tags=["home"], writer="owner")
    eng.recall("where does the user live?")          # counter flush -> 'access'
    eng.reflect(writer="claude-code")                # dedup -> 'reflect'
    eng.forget(c.id, writer="owner")

    events = list(eng.journal.events())
    ops = [e["op"] for e in events]
    for op in ("remember", "update", "access", "reflect", "forget"):
        assert op in ops, f"{op} missing from journal: {ops}"
    for e in events:
        assert {"ts", "writer", "op", "memory_id"} <= set(e)
        if e["op"] != "forget":
            assert e["content_hash"] and e["meta_hash"]
    assert any(e["op"] == "forget" and e["memory_id"] == c.id for e in events)

    # replaying the journal reproduces the notes on disk exactly
    replayed = {mid: m.to_json() for mid, m in replay_states(events).items()}
    assert replayed == disk_state(eng)
    eng.close()


def test_journal_is_append_only(tmp_path):
    eng = make_engine(tmp_path)
    eng.remember("First.")
    before = eng.journal.path.read_bytes()
    eng.remember("Second.")
    after = eng.journal.path.read_bytes()
    assert after.startswith(before) and len(after) > len(before)
    eng.close()


# --------------------------------------------------------------------------- #
# hammer test: many concurrent writers, no corruption, no lost writes
# --------------------------------------------------------------------------- #
def test_hammer_concurrent_writers(tmp_path):
    eng = make_engine(tmp_path, counter_flush_every=3)
    writers = ["owner", "claude-code", "grok-sales-1", "grok-support-1"]
    n_threads, n_ops = 8, 25
    created = [[] for _ in range(n_threads)]
    errors = []

    def worker(t):
        w = writers[t % len(writers)]
        try:
            for i in range(n_ops):
                mem = eng.remember(f"Fact {i} from thread {t} about topic {i % 5}.",
                                   entities=[f"topic{i % 5}"], writer=w)
                created[t].append(mem.id)
                eng.recall(f"topic {i % 5}", k=3, writer=w)
                if i % 3 == 0:
                    eng.update(mem.id, confidence=0.8, writer=w)
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    eng.flush_access()

    all_ids = [mid for ids in created for mid in ids]
    assert len(all_ids) == len(set(all_ids)) == n_threads * n_ops
    on_disk = disk_state(eng)
    assert set(on_disk) == set(all_ids), "lost or phantom writes"
    assert eng.index.stats()["total"] == len(all_ids)
    # every note parses, carries its writer, and the index agrees with disk
    sync = eng.sync()
    assert sync["embedded"] == 0 and sync["meta_updated"] == 0 and sync["removed"] == 0
    # the journal is complete and replays to exactly the disk state
    events = list(eng.journal.events())
    assert sum(e["op"] == "remember" for e in events) == len(all_ids)
    assert {mid: m.to_json() for mid, m in replay_states(events).items()} == on_disk
    eng.close()


def test_hammer_through_gateway(gateway):
    eng, base = gateway
    who = ["grok-sales-1", "claude-code"]
    n_threads, n_ops = 6, 15
    errors = []

    def worker(t):
        tok = TOKENS[who[t % 2]]
        for i in range(n_ops):
            st, body = _post(base + "/remember", {"text": f"Gateway fact {t}-{i}."}, tok)
            if st != 200:
                errors.append((st, body))
            st, body = _post(base + "/recall", {"query": f"gateway fact {t}"}, tok)
            if st != 200:
                errors.append((st, body))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]
    mems = list(eng.store.iter_memories())
    assert len(mems) == n_threads * n_ops
    assert {m.writer for m in mems} == set(who)
