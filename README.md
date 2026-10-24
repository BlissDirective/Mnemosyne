# Mnemosyne

A **local-first, portable, evolutionary** memory engine for AI agents, exposed
over MCP so Claude Desktop and Claude Code can remember across sessions.

The whole design in one line: **the source of truth is plain Markdown you own;
the searchable index is a rebuildable cache derived from it.** That single choice
is what makes "seamless local + remote" tractable and what lets you upgrade
embedding models without ever losing data.

---

## Why it's built this way

Five well-known systems inspired this, and each contributes one idea:

| Idea | Borrowed from | Where it lives here |
|---|---|---|
| Keep raw content verbatim, never lossy | MemPalace | `archive/` + `archive_text` |
| Distil durable facts so recall stays cheap | Mem0 | `remember` writes distilled statements |
| Multi-strategy retrieval + consolidation | Hindsight | `retrieval.py`, `consolidate.py` |
| A typed memory taxonomy | ALucek / CoALA | `MemoryType` |
| Plain files + links as the substrate | Obsidian | the `notes/` folder is Obsidian-ready |

### Two layers

1. **Canonical (source of truth):** one Markdown file per memory in `notes/`,
   with YAML frontmatter for structured fields. Human-readable, diff-able, and
   it syncs by *any* file method because it's just text. Raw transcripts go in
   `archive/`, unsummarized, so nothing is ever truly lost. Archive references
   are stored **relative to the memory root**, so they survive syncing to
   another machine.
2. **Derived (rebuildable cache):** a single SQLite file in `.index/` (WAL
   mode) holding metadata, an FTS5 keyword index, float32 vectors (searched
   with numpy — no fragile native extensions), and an entity co-occurrence
   graph. Delete it and rebuild any time with `reindex`. Sync is incremental
   and three-way: text changes re-embed; frontmatter-only changes (confidence,
   links, validity, access counters — e.g. from `reflect`, counter flushes, or
   hand edits) get a cheap metadata refresh with no re-embedding; everything
   else is skipped.

### The "evolutionary" part

Every vector records which model produced it. When a better embedding model
ships, point the config at it and run `reindex --force`: because the canonical
text is preserved, re-embedding is a rebuild, not a migration risk (embedding
requests are batched and retried, so migrating hundreds of memories is
routine). `reflect` adds slow self-improvement — it links duplicates,
reinforces corroborated memories, and (optionally) decays stale ones. Once a
memory is marked as a superseded duplicate, recall hides it by default — so
consolidation actually cleans what you see, not just what's on disk. And
because every recall is counted, decay is **access-gated**: a memory the agent
keeps using never fades, however old it is.

---

## Layout

```
mnemosyne/
├── mnemosyne/
│   ├── schema.py       Memory + MemoryType, Markdown (de)serialization
│   ├── store.py        canonical Markdown store + verbatim archive
│   ├── embeddings.py   pluggable providers: dev / openai / voyage (+ your own)
│   ├── index.py        SQLite + FTS5 + numpy vectors + entity graph; reindex()
│   ├── retrieval.py    semantic + keyword + graph (+ gated recency), weighted RRF
│   ├── consolidate.py  reflect(): dedup, reinforce, access-gated decay
│   ├── engine.py       update-in-place, access counters, prime(); wires it all
│   ├── server.py       FastMCP/MCPServer: the memory_* tools (mcp 1.x & 2.x), stdio + HTTP
│   ├── auth.py         writer identity, bearer-token resolution
│   ├── rings.py        ring access policy (private / team / proposed / canon)
│   ├── journal.py      append-only audit journal (journal/events.jsonl)
│   ├── gateway.py      JSON/HTTP adapter for non-MCP agents
│   └── cli.py          same operations for humans, sync hooks, and session hooks
├── tests/test_pipeline.py      offline suite: core engine
├── tests/test_identity.py      identity, HTTP auth, journal, hammer tests
├── tests/test_rings.py         rings, quarantine, promotion workflow
├── tests/live_stdio_check.py   real MCP client <-> server round trip
├── config.example.toml
├── pyproject.toml
└── requirements.txt
```

---

## Quickstart (works offline, zero setup)

```bash
cd mnemosyne
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# store and retrieve — uses the offline 'dev' embedding provider by default
mnemosyne remember "User prefers concise answers and minimal formatting." --type preference
mnemosyne recall "how does the user like responses?"
mnemosyne stats
```

Your memory lives in `~/mnemosyne/` by default (`notes/`, `archive/`, `.index/`).
Change it with `MNEMOSYNE_HOME` or `--root`.

Run the tests:

```bash
python -m pytest -q                      # offline suite
python tests/live_stdio_check.py         # real MCP stdio round trip
```

---

## Switch to quality embeddings

The `dev` provider is for trying things offline; it isn't competitive for real
retrieval. For quality, use an API model. Create `~/mnemosyne/config.toml`:

```toml
[embedding]
provider = "openai"                 # or "voyage"
model = "text-embedding-3-large"    # voyage: "voyage-3-large"
# dim = 1024                        # OpenAI supports Matryoshka truncation
```

…and set the key in your environment:

```bash
export OPENAI_API_KEY="sk-..."      # or VOYAGE_API_KEY for voyage
mnemosyne reindex --force           # re-embed everything with the new model
```

With Voyage, recall queries are embedded with `input_type="query"` and stored
memories with `input_type="document"` — Voyage's models are asymmetric, and
mixing the two hurts retrieval quality.

Benchmark candidate models on your own data — leaderboard scores don't always
transfer. Want it fully private instead? Add a local provider (see *Extending*
below) backed by a `sentence-transformers` model such as BGE-M3 or
Qwen3-Embedding.

---

## Recall behavior worth knowing

- **Recency is gated.** The recency strategy only joins the fusion when the
  query actually has temporal cues ("recent", "yesterday", "last week", …) —
  and even then at reduced weight (`recency_weight`, default 0.3). A memory
  stored five minutes ago no longer pollutes unrelated queries.
- **Superseded and expired memories are hidden.** Anything `reflect` marked
  `duplicate_of`, and any `world_fact` past its `valid_to`, is filtered out of
  recall by default. Pass `include_superseded=true` (MCP) or
  `--include-superseded` (CLI) to see everything.
- **Usage is counted.** Every memory a recall returns gets `recall_count`
  bumped and `last_recalled` stamped — in the index immediately, and written
  back to the canonical frontmatter in batches (`[access] flush_every`,
  default 10; also flushed on reflect/reindex/exit) so a Git-synced store
  isn't churned by every lookup. Counters feed decay gating, `prime`, and
  your own curation (`never_recalled` in stats shows dead weight).

---

## Connect to Claude Desktop

1. Open **Settings → Developer → Edit Config** (creates the file if needed).
   It lives at:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - Linux: `~/.config/Claude/claude_desktop_config.json`
2. Add the server (use the **absolute path** to your venv's Python — Claude
   launches subprocesses with a minimal PATH, so bare `python` may not resolve):

```json
{
  "mcpServers": {
    "memory": {
      "command": "/ABSOLUTE/PATH/mnemosyne/.venv/bin/python",
      "args": ["-m", "mnemosyne.server"],
      "env": {
        "MNEMOSYNE_HOME": "/ABSOLUTE/PATH/to/your/memory",
        "MNEMOSYNE_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

3. Restart Claude Desktop. You should now have `memory_remember`,
   `memory_recall`, `memory_update`, `memory_reflect`, `memory_forget`,
   `memory_stats`, and `memory_reindex` available.

> The server runs on both generations of the MCP Python SDK: it imports
> `MCPServer` on mcp 2.x and falls back to `FastMCP` on mcp 1.x.

---

## Connect to Claude Code

From your terminal (not inside the Claude Code REPL). `--scope user` makes it
available across all your projects:

```bash
claude mcp add --transport stdio --scope user \
  --env MNEMOSYNE_HOME=/ABSOLUTE/PATH/to/your/memory \
  --env MNEMOSYNE_PROVIDER=openai \
  --env OPENAI_API_KEY=sk-... \
  memory \
  -- /ABSOLUTE/PATH/mnemosyne/.venv/bin/python -m mnemosyne.server

claude mcp list          # verify it connected
```

All flags (`--transport`, `--scope`, `--env`) come **before** the server name;
`--` separates them from the launch command. Prefer editing files? The same
config can go in a project-scoped `.mcp.json` (which supports `${VAR}`
expansion so secrets stay in your shell environment, out of version control).

> Tip: `mnemosyne remember`/`recall` from the CLI and the `memory_*` tools in
> Claude share the **same store** — so you can seed or inspect memories by hand
> and Claude sees them immediately (after a `reindex`, or on its next startup).

### Auto-recall at session start (hook)

Claude Code adds a `SessionStart` hook's plain-text stdout to Claude's context,
so `mnemosyne prime` can inject your best standing memories at the top of every
session — no need to remember to recall. Add to `~/.claude/settings.json`
(merge into any existing `hooks` key):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/ABSOLUTE/PATH/mnemosyne/.venv/bin/mnemosyne prime",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

Notes that matter:

- `prime` prints a compact block — store stats plus the top memories ranked by
  confidence × actual usage × recency, with superseded and expired entries
  filtered — and labels it as **stored data, not instructions**.
- SessionStart stdout is capped at **10,000 characters**; `prime` stays safely
  under it (~9,000). `mnemosyne prime "some topic"` primes topically instead.
- Omitting `matcher` fires on every session source (startup, resume, clear,
  compact, and fork on recent builds). Add `"matcher": "startup"` to prime only
  brand-new sessions.
- `prime` is hook-safe: errors go to stderr and it always exits 0, so a memory
  problem can never block a session from starting.
- Cloud sessions don't read your local `~/.claude/settings.json`; this hook is
  for Claude Code running on your machine.

---

## Rings: private notebooks, team memory, a reviewed canon

Every memory lives in a **ring**, and the folder layout mirrors it:

```
notes/canon/                reviewed organizational truth
notes/proposed/             the review queue (quarantine)
notes/team/<name>/          shared by a team, e.g. team:sales
notes/private/<writer>/     one agent's working notebook
```

| | write | read |
|---|---|---|
| `private:<you>` | you | you (and the owner, tier 3) |
| `team:<name>` | tier ≥ 2, or a tier-1 member | members, tier ≥ 2 |
| `proposed` | anyone | reviewers (tier ≥ 2) |
| `canon` | tier ≥ 2 | everyone |

A write goes to the writer's private ring unless `ring` says otherwise. When a
writer below tier 2 aims at `canon` or another team's ring, the write is
**quarantined**: it lands in `proposed` with its intended ring recorded, and
default recall cannot see it. It becomes shared truth only when a
**different** writer of tier ≥ 2 promotes it:

```bash
MNEMOSYNE_WRITER=grok-sales-1 mnemosyne remember "Refunds take 60 days." --ring canon
#   -> stored … ring=proposed (quarantined for review)
MNEMOSYNE_WRITER=claude-code mnemosyne queue                # the review queue
MNEMOSYNE_WRITER=claude-code mnemosyne promote <id>         # or: reject <id> --reason "…"
```

`memory_propose` is the organizational merge request. Use it to share a
private note (`id`), to suggest a fix to a canon memory you cannot edit
(`id` + corrected `text`), or to submit something new. When a corrected copy is
promoted, the original is marked superseded. Reviewers see the queue depth and
the oldest proposals in every `prime`.

Team membership is declared on the writer:
`grok-sales-1 = { tier = 1, teams = ["sales"] }`. Recall searches canon, your
private ring, and your teams by default. Pass `scope` (`["team:ops"]`,
`["proposed"]`, `["all"]`) to search further, within what your tier may read.
`reflect` never merges or corroborates across rings, so an agent repeating
itself in its private ring cannot move canon.

---

## Many writers: identity, HTTP transport, audit journal

With more than one agent writing, *who wrote this* matters more than anything
else in a memory. Every memory records its `writer` (a stable id such as
`owner`, `claude-code`, or `grok-sales-1`), and every mutation is appended to
`journal/events.jsonl`.

**Declare your writers** in `config.toml` (tiers: 0 untrusted, 1 standard,
2 senior, 3 owner; anyone not listed is tier 0):

```toml
[writers]
owner        = { tier = 3 }
claude-code  = { tier = 2 }
grok-sales-1 = { tier = 1 }
```

**Local use (stdio / CLI)** runs as `MNEMOSYNE_WRITER` (default `owner`).

**Several agents at once: MCP over HTTP.** Give each writer a token in the
environment (never in a file) and start the server:

```bash
export MNEMOSYNE_TOKEN_CLAUDE_CODE="$(openssl rand -hex 32)"
export MNEMOSYNE_TOKEN_GROK_SALES_1="$(openssl rand -hex 32)"
mnemosyne-server --http --host 127.0.0.1 --port 8787     # MCP at /mcp
```

Each request must send `Authorization: Bearer <token>`; the token decides the
writer, and an unknown or missing token gets `401` before anything touches the
store. Keep it on localhost or behind a TLS proxy.

**Agents that can't speak MCP** use the JSON gateway (same tokens, no logic of
its own):

```bash
mnemosyne-gateway --port 8788
curl -s localhost:8788/remember -H "Authorization: Bearer $MNEMOSYNE_TOKEN_GROK_SALES_1" \
     -d '{"text": "Initech asked for a demo next week.", "type": "episodic"}'
```

Routes: `POST /remember`, `POST /recall`, `POST /update`, `GET /health`.

**The journal.** `journal/events.jsonl` is canonical and syncs with `notes/`.
Each line is `{ts, writer, op, memory_id, content_hash, meta_hash, state}`
for every remember, update, forget, reflect edit, and counter flush. Replaying
it reproduces the notes; `mnemosyne journal -n 20` shows the latest events.

---

## Local + remote sync (seamless, and you own it)

Because the canonical layer is plain text and the index is disposable, sync is
simple and robust:

```bash
cd ~/mnemosyne
git init && git add notes archive journal config.example.toml && git commit -m "memory"
git remote add origin <your private repo>
git push -u origin main
```

The included `.gitignore` **excludes `.index/`** (the rebuildable cache) and
`config.toml` (which may hold model choices) — so only your portable text
syncs. On another machine:

```bash
git pull
mnemosyne reindex        # regenerate the local index from the notes
```

That's it: canonical memory is identical everywhere; each device keeps its own
index. Frontmatter changes that arrive via sync (confidence, links, validity,
access counters) are picked up by `reindex` as cheap metadata refreshes — no
re-embedding. Prefer zero-thought sync? Point `MNEMOSYNE_HOME` at a
Dropbox/iCloud/Syncthing folder instead of using Git. At large scale, where
rebuilding the index per device gets slow, swap the derived store for an
embedded-replica DB (libSQL/Turso) or add Litestream — the canonical layer
doesn't change.

---

## The 7 tools

| Tool | Does | Notes |
|---|---|---|
| `memory_remember` | store one memory | pass `type`, `entities`, optional `archive_text` |
| `memory_recall` | retrieve relevant memories | semantic + keyword + graph (+ gated recency), weighted RRF; hides superseded/expired by default; records usage |
| `memory_update` | edit a memory in place | preserves id, created, links, counters; text change re-embeds; `valid_to` in the past retires a fact; `""` clears validity |
| `memory_reflect` | consolidate | `dry_run=true` to preview duplicate clusters; decay skips anything recently updated or recalled |
| `memory_forget` | delete by id | destructive — prefer `memory_update` for corrections |
| `memory_stats` | counts + model coverage | shows `needs_reembed` backlog, `superseded`, and `never_recalled` |
| `memory_reindex` | rebuild index from notes | incremental (text vs metadata); `force=true` re-embeds after a model swap |

---

## Extending

- **A local (private) embedding provider:** subclass `EmbeddingProvider` in
  `embeddings.py`, implement `_embed` (and optionally `embed_query` if the
  model is asymmetric), and register it in `get_provider`. A
  `sentence-transformers` model like `BAAI/bge-m3` is ~15 lines.
- **LLM-assisted consolidation:** `reflect(dry_run=True)` surfaces duplicate
  clusters. The natural loop: Claude reads a cluster, rewrites the survivor
  with `memory_update`, and `memory_forget`s the rest. This is where the agent
  actively curates its own memory.
- **Typed relations:** the entity graph is co-occurrence today; add a
  `relations(src, rel, dst, valid_from, valid_to)` table for real edges.
- **Scale:** numpy brute-force cosine is instant into the tens of thousands of
  memories, and `reflect` computes similarities in row blocks so its memory use
  stays flat. Beyond that, add sqlite-vec or FAISS behind `index.semantic()`.

---

## One honest note on reach

In **Claude Desktop / Claude Code**, this server runs on your machine and Claude
calls the tools directly — that's where "remote memory Claude can access" fully
works. In the **web chat at claude.ai**, Claude can't reach your machine between
turns, so there you'd paste a recall or use the built-in memory. The custom
system's home is Desktop/Code (and any device they sync to).

Memory here means a retrievable store the agent reads fresh each session — not
lived, felt continuity. It makes the *work* continuous and portable, which is
the useful thing to build.
