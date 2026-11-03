"""v1.2: runtime roster reload, token digests, and the memory_prime tool.

All offline. HTTP tests bind 127.0.0.1 on a free port in-process."""

import hashlib
import json
import os
import signal
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from mnemosyne.auth import is_armed, resolve_token, token_env_name
from mnemosyne.config import Config, WriterSpec, load_config, load_writers
from mnemosyne.engine import MemoryEngine, install_sighup_reload


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def write_config(root, writers_toml: str) -> None:
    (root / "config.toml").write_text(
        '[embedding]\nprovider = "dev"\n\n[writers]\n' + writers_toml)


@pytest.fixture
def eng(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("MNEMOSYNE_TOKEN_"):
            monkeypatch.delenv(k)
    monkeypatch.delenv("MNEMOSYNE_WRITER", raising=False)
    write_config(tmp_path, 'owner = { tier = 3 }\n'
                           f'agent-a = {{ tier = 1, teams = ["sales"], '
                           f'token_sha256 = "{sha("tok-a")}" }}\n')
    e = MemoryEngine(load_config(str(tmp_path)))
    yield e
    e.close()


# ---- config + auth ---------------------------------------------------------
def test_token_sha256_parses_and_authenticates(eng):
    spec = eng.cfg.writers["agent-a"]
    assert spec.token_sha256 == sha("tok-a") and spec.teams == ["sales"]
    ident = resolve_token(eng.cfg, "tok-a", environ={})
    assert ident is not None and ident.writer == "agent-a" and ident.tier == 1
    assert resolve_token(eng.cfg, "tok-wrong", environ={}) is None
    # the digest itself is not a token
    assert resolve_token(eng.cfg, sha("tok-a"), environ={}) is None


def test_env_tokens_still_work_alongside_digests(eng):
    env = {token_env_name("owner"): "tok-owner"}
    assert resolve_token(eng.cfg, "tok-owner", environ=env).writer == "owner"
    assert is_armed(eng.cfg, "owner", environ=env)
    assert is_armed(eng.cfg, "agent-a", environ={})
    assert not is_armed(eng.cfg, "owner", environ={})


def test_bad_digest_is_rejected(tmp_path):
    write_config(tmp_path, 'x = { tier = 1, token_sha256 = "not-hex" }\n')
    with pytest.raises(ValueError, match="token_sha256"):
        load_config(str(tmp_path))


def test_load_writers_requires_file_and_table(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_writers(tmp_path)
    (tmp_path / "config.toml").write_text('[embedding]\nprovider = "dev"\n')
    with pytest.raises(ValueError, match="no \\[writers\\]"):
        load_writers(tmp_path)


# ---- reload ----------------------------------------------------------------
def test_owner_reload_adds_changes_and_removes(eng, tmp_path):
    write_config(tmp_path, 'owner = { tier = 3 }\n'
                           f'agent-a = {{ tier = 2, token_sha256 = "{sha("tok-a")}" }}\n'
                           f'agent-b = {{ tier = 1, token_sha256 = "{sha("tok-b")}" }}\n')
    assert resolve_token(eng.cfg, "tok-b", environ={}) is None
    res = eng.reload_roster(writer="owner")
    assert res["added"] == ["agent-b"] and res["changed"] == ["agent-a"]
    assert res["removed"] == []
    assert resolve_token(eng.cfg, "tok-b", environ={}).writer == "agent-b"
    assert eng.identity("agent-a").tier == 2

    write_config(tmp_path, 'owner = { tier = 3 }\n')
    res = eng.reload_roster(writer="owner")
    assert res["removed"] == ["agent-a", "agent-b"]
    assert resolve_token(eng.cfg, "tok-a", environ={}) is None
    assert eng.identity("agent-a").tier == 0      # fail closed once removed


def test_non_owner_cannot_reload(eng, tmp_path):
    write_config(tmp_path, 'owner = { tier = 3 }\nagent-a = { tier = 3 }\n')
    with pytest.raises(PermissionError, match="owner"):
        eng.reload_roster(writer="agent-a")
    assert eng.identity("agent-a").tier == 1      # unchanged


def test_invalid_config_keeps_current_roster(eng, tmp_path):
    before = dict(eng.cfg.writers)
    (tmp_path / "config.toml").write_text("[writers]\nagent-a = { tier = 9 }\n")
    with pytest.raises(ValueError):
        eng.reload_roster(writer="owner")
    assert eng.cfg.writers == before
    (tmp_path / "config.toml").write_text("this is = = not toml")
    with pytest.raises(Exception):
        eng.reload_roster(writer="owner")
    assert eng.cfg.writers == before


def test_reloaded_writer_writes_with_its_tier(eng, tmp_path):
    write_config(tmp_path, 'owner = { tier = 3 }\n'
                           f'agent-b = {{ tier = 1, token_sha256 = "{sha("tok-b")}" }}\n')
    eng.reload_roster(writer="owner")
    mem = eng.remember("Initech wants a demo.", type="episodic", writer="agent-b",
                       ring="canon")
    assert mem.writer == "agent-b" and mem.ring == "proposed"   # quarantined at tier 1


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this OS")
def test_sighup_reloads(eng, tmp_path):
    old = signal.getsignal(signal.SIGHUP)
    try:
        assert install_sighup_reload(lambda: eng)
        write_config(tmp_path, 'owner = { tier = 3 }\n'
                               f'agent-c = {{ tier = 1, token_sha256 = "{sha("tok-c")}" }}\n')
        os.kill(os.getpid(), signal.SIGHUP)
        for _ in range(50):
            if "agent-c" in eng.cfg.writers:
                break
            time.sleep(0.01)
        assert resolve_token(eng.cfg, "tok-c", environ={}).writer == "agent-c"
        # a broken file on SIGHUP keeps the roster
        (tmp_path / "config.toml").write_text("[writers]\nagent-c = { tier = 7 }\n")
        os.kill(os.getpid(), signal.SIGHUP)
        time.sleep(0.05)
        assert "agent-c" in eng.cfg.writers
    finally:
        signal.signal(signal.SIGHUP, old)


# ---- MCP tools over authenticated HTTP --------------------------------------
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(eng, monkeypatch):
    import uvicorn
    from mnemosyne import server
    monkeypatch.setenv(token_env_name("owner"), "tok-owner")
    old = server._engine
    server._engine = eng
    port = free_port()
    config = uvicorn.Config(server.http_app(), host="127.0.0.1", port=port,
                            log_level="warning")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    t.join(timeout=5)
    server._engine = old


def call_tool(url: str, token: str, name: str, arguments: dict):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=10) as r:
        payload = json.loads(r.read())
    return payload["result"]["content"][0]["text"]


def test_prime_tool_returns_the_callers_block(http_server, eng):
    eng.remember("User prefers concise answers.", type="preference", writer="agent-a")
    eng.remember("Owner-only private note.", type="semantic", writer="owner")
    text = call_tool(http_server, "tok-a", "memory_prime", {"params": {}})
    assert "concise answers" in text
    assert "Owner-only private note" not in text          # not in agent-a's scope
    assert len(text) <= 9000
    topical = call_tool(http_server, "tok-a", "memory_prime",
                        {"params": {"query": "how should answers be written?",
                                    "k": 3, "max_chars": 1000}})
    assert "concise answers" in topical and len(topical) <= 1000


def test_reload_tool_is_owner_only_and_takes_effect(http_server, tmp_path):
    write_config(tmp_path, 'owner = { tier = 3 }\n'
                           f'agent-a = {{ tier = 1, token_sha256 = "{sha("tok-a")}" }}\n'
                           f'agent-d = {{ tier = 1, token_sha256 = "{sha("tok-d")}" }}\n')
    denied = call_tool(http_server, "tok-a", "memory_reload_roster", {})
    assert "cannot reload" in denied
    with pytest.raises(urllib.error.HTTPError) as e:
        call_tool(http_server, "tok-d", "memory_stats", {})
    assert e.value.code == 401

    ok = json.loads(call_tool(http_server, "tok-owner", "memory_reload_roster", {}))
    assert ok["added"] == ["agent-d"]
    assert '"total"' in call_tool(http_server, "tok-d", "memory_stats", {})
