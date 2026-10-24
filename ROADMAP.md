# ROADMAP.md — Mnemosyne: from one mind to an institution

**What this file is.** The build plan for evolving Mnemosyne v0.3.0 (single-user
memory engine, see `mnemosyne_bundle.md`) into shared organizational memory for
a mixed team of AI agents — Grok bot agents + Claude Code — running a real
business under human ownership. It is written to be executed by Claude Code,
phase by phase, in a repo exploded from the bundle.

**How to execute this file (instructions to the building agent):**
1. Work one phase at a time, in order. Do not start a phase until the previous
   phase's *Definition of done* is fully green.
2. Each phase is a shippable release. Bump the version, keep the full test
   suite passing, and keep `tests/live_stdio_check.py` passing on every phase.
3. The **Invariants** section below overrides anything else in this file. If a
   phase step would break an invariant, stop and flag it instead of building it.
4. Conventions in `CLAUDE.md` ("Working on this codebase") continue to apply to
   every phase.
5. When a step says *human gate*, the work stops and the owner decides. These
   gates are the point of the design — do not engineer around them.

---

## Invariants (true in every phase, non-negotiable)

- **Canonical/derived split.** Plain Markdown (+ the event journal, Phase 1) is
  the source of truth; everything in `.index/` is rebuildable. Any new feature
  must survive `rm -rf .index/ && mnemosyne reindex`.
- **Owner-readable substrate.** The owner can read, diff, Git-sync, and walk
  away with the entire memory with no proprietary tooling. No feature may move
  canonical state into an opaque store.
- **Evolutionary embeddings.** Every vector records `embed_model`; model swaps
  are a `reindex --force`, never a migration.
- **meta_hash write-through.** Any code path that changes frontmatter writes
  through to the index or is caught by reindex. (The project's worst historical
  bug — never reintroduce it.)
- **Memories are data, not instructions.** Every surface that returns memory
  text (recall, prime, digests) labels it as stored data. Exception introduced
  in Phase 3: `policy` memories, which are explicitly instruction-like and
  therefore get the *strictest* write controls in the system.
- **Fail open for reads, fail closed for writes.** A broken index must never
  block recall of the canonical files; an unauthenticated or unauthorized write
  must never land.
- **Dependencies stay light** (`mcp`, `numpy`, `pyyaml`, `httpx`). No vector
  DB, no message broker, no orchestration framework unless a phase's acceptance
  tests cannot pass without it.

---

## Phase 0 — Repo bootstrap (v0.3.0, half a day)

**Goal:** the bundle becomes a working repo with CI.

Steps:
1. Explode `mnemosyne_bundle.md` into the file tree it documents (the bundle's
   *File tree* section is authoritative). `pip install -e .`, run
   `python -m pytest -q` (22 tests) and `tests/live_stdio_check.py`.
2. `git init`, commit, add a GitHub Actions workflow: pytest on 3.11–3.13,
   plus the live stdio check.
3. Tag `v0.3.0`.

**Definition of done:** CI green on a fresh clone; both test entry points pass.

---

## Phase 1 — Identity, transport, audit (v0.4.0, ~2–3 days)

The single most important phase. With more than one writer, *who wrote this* is
the most important field in the schema, and shared memory becomes the command
bus of the business — so identity, authenticated transport, and an append-only
audit trail come before any collaboration features.

**1.1 Writer identity**
- `schema.py`: add `writer: str = "owner"` (stable agent id, e.g. `grok-sales-1`,
  `claude-code`, `owner`). Keep `source` as the channel (conversation/cli/import).
  Include `writer` in `meta_hash` and in recall/prime output lines.
- `config.toml` gains:
  ```toml
  [writers]
  # token-env = writer id : trust tier (0 untrusted, 1 standard, 2 senior, 3 owner)
  owner        = { tier = 3 }
  claude-code  = { tier = 2 }
  grok-sales-1 = { tier = 1 }
  ```
  Tokens themselves live in env vars (`MNEMOSYNE_TOKEN_<WRITER>`), never in files.

**1.2 Authenticated multi-client transport**
- stdio remains the default for local single-agent use; identity comes from
  `MNEMOSYNE_WRITER` env.
- Add `mnemosyne-server --http --host 127.0.0.1 --port 8787`: the MCP SDK's
  streamable HTTP transport, bearer-token auth resolving to a writer id. Every
  tool call executes with that writer's identity and tier. Unknown token →
  reject before any engine call.
- If a Grok bot framework cannot speak MCP, add a thin `gateway.py` (stdlib
  `http.server` or the SDK's ASGI app — no new heavy deps) mapping
  `POST /remember|/recall|/update` onto the engine with the same token auth.
  The gateway is an adapter only; no logic lives in it.
- Concurrency note: multiple writers → verify the WAL + single-lock model under
  parallel HTTP calls with a hammer test (N threads × M ops, no corruption, no
  lost writes).

**1.3 Append-only audit journal**
- New canonical artifact `journal/events.jsonl` (Git-synced like `notes/`):
  one line per mutation — `{ts, writer, op, memory_id, content_hash, meta_hash}`
  for remember/update/forget/promote/resolve, plus `recall_for_decision`
  entries when Phase 7 guardrails fire.
- The journal is append-only by convention and verified by test: replaying it
  must reproduce the current `notes/` state for the ops it covers (full event
  sourcing arrives in Phase 6; this phase only guarantees the trail exists and
  is complete).

**Definition of done:** every memory carries a writer; writes without a valid
token are rejected over HTTP; two concurrent writers pass the hammer test;
every mutation appears in the journal; all prior tests still green.

---

## Phase 2 — Memory rings and promotion (v0.5.0, ~3 days)

**Goal:** agents get private working memory; the organization gets a reviewed
canon; low-trust writers physically cannot pollute shared recall.

**2.1 Rings**
- `schema.py`: add `ring: str` ∈ `private:<writer>` | `team:<name>` | `proposed`
  | `canon`. Default ring for a write = the writer's private ring.
- Filesystem mirrors the ring: `notes/canon/`, `notes/proposed/`,
  `notes/team/<name>/`, `notes/private/<writer>/` — so the owner can see the
  whole topology with `ls`.
- `memory_recall` gains `scope` (default: `canon` + the caller's own private
  ring + their teams). Reading another agent's private ring requires tier 3.

**2.2 Promotion workflow (the organizational merge request)**
- New tools:
  - `memory_propose(id | new memory)` → copies/creates in `proposed`, records
    proposer.
  - `memory_promote(id, target_ring)` → requires tier ≥ 2 and
    promoter ≠ proposer (no self-promotion); moves to `canon`/`team:*`,
    journal-logged.
  - `memory_reject(id, reason)` → returns it to the proposer's private ring
    with the reason appended as a tag.
- Tier rules: tier 0–1 writers **cannot write canon directly** — their canon-
  bound writes land in `proposed` automatically (quarantine). Tier 2 can write
  `team:*` directly and promote; tier 3 can do anything.
- `prime` gains a reviewer view: pending `proposed` count and the oldest items,
  so review debt is visible at every session start.

**Definition of done:** a tier-1 writer's canon write lands in `proposed`; it
is invisible to default recall until promoted; promotion requires a different,
senior writer; all transitions in the journal; ring survives reindex.

---

## Phase 3 — Business memory types: decisions, commitments, policies, claims (v0.6.0, ~3–4 days)

**Goal:** the taxonomy a business actually runs on, plus contradiction
surfacing — so agents stop relitigating settled decisions and stop silently
holding incompatible beliefs.

**3.1 New `MemoryType`s**
- `decision` — body convention: statement, rationale, alternatives rejected,
  revisit-when condition. Promotion to canon required before any agent may act
  on it.
- `commitment` — adds frontmatter `due: ISO-date` and `owner_agent`. `prime`
  surfaces commitments due within 7 days or overdue, always, at the top.
- `policy` — a standing constraint agents must check before acting ("never
  discount below 20%", "all outbound email in brand voice X"). **Tier 3 writes
  only.** `recall` renders policies in a separate, clearly-labeled section.
  This is the one deliberate exception to data-not-instructions, and the write
  restriction is what makes the exception safe.
- `hypothesis` — a falsifiable belief with `review_by: ISO-date` (consumed in
  Phase 4).

**3.2 Typed relations**
- New index table `relations(src, rel, dst)`, rel ∈ `supports | contradicts |
  supersedes | evidence_for`, derived from a `relations:` frontmatter list
  (canonical, so it syncs and survives rebuilds). `links:` stays as untyped
  association for back-compat.

**3.3 Contradiction surfacing (deterministic, conservative)**
- Extend `reflect()`: candidate contradictions = same type + shared entities +
  cosine in a mid band (configurable, default 0.70–0.94: related but not
  duplicate) + not already related. Report them in `ReflectReport.contested`;
  **never auto-resolve**. Resolution is a librarian/owner act: mark one
  superseded, or record an explicit `contradicts` relation with a note.
- `memory_recall` flags results that have an unresolved `contradicts` relation
  (`⚠ contested` in output) so no agent acts on a contested belief unknowingly.

**Definition of done:** commitments appear in prime by due date; a tier-1 agent
cannot write a policy; reflect surfaces a planted contradiction and does not
auto-resolve it; contested memories are flagged in recall; relations survive
reindex.

---

## Phase 4 — Outcome calibration and writer reputation (v0.7.0, ~2–3 days)

**Goal:** confidence measures *truth*, not popularity. Today confidence rises
from corroboration and usage; a business needs it calibrated against reality.

- New tool `memory_resolve(id, outcome: confirmed|refuted|mixed, note)` for
  `hypothesis` (and optionally `decision`) memories:
  - confirmed → confidence up; refuted → memory gets `valid_to = today`
    (retired, not deleted — the history is the lesson) and a `refuted` tag.
  - Every resolution updates the **writer's reputation**: a per-writer Brier-style
    running score stored canonically in `notes/canon/_writers/<writer>.md`
    (owner-readable like everything else) and mirrored in the index.
- `prime` lists hypotheses past `review_by` as "awaiting resolution".
- Recall ranking: writer reputation becomes a mild prior on the confidence
  term (bounded, e.g. ×0.8–1.2 — reputation nudges, never dominates).
- `reflect()` reinforcement (corroboration boost) is capped lower than
  resolution boosts, so repeated assertion can never outrank verified outcome.

**Definition of done:** resolving a hypothesis moves writer reputation; a
refuted memory drops out of default recall but remains auditable; reputation
visibly (and boundedly) affects ranking in a test; reputations survive reindex.

---

## Phase 5 — The librarian: LLM-assisted curation and digests (v0.8.0, ~3 days)

**Goal:** consolidation grows from deterministic dedup into a staffed
organizational function.

- `LIBRARIAN.md` protocol in the repo — the system prompt/runbook for a
  scheduled Claude Code session (cron/scheduled task) that connects as writer
  `librarian` (tier 2) and each run:
  1. `memory_reflect dry_run=true` → merge duplicate clusters via
     `memory_update` on the survivor + `memory_forget` the rest.
  2. Reviews `ReflectReport.contested` → proposes resolutions (promote one,
     supersede the other) as `proposed` entries for owner sign-off.
  3. Reviews the `proposed` queue (promote clean facts, reject noise, with
     reasons).
  4. Writes a **digest**: a `semantic` memory tagged `digest:<ISO-week>`
     summarizing the week's decisions, resolutions, and open questions.
- `mnemosyne curate` CLI: runs steps the protocol can do deterministically
  (cluster merge candidates, queue stats) and emits a work list, so the
  librarian session spends its tokens on judgment, not discovery.
- Tiered retrieval: recall consults digests first for queries with broad scope
  (heuristic: no entities + high k), descending to raw memories on demand.

**Definition of done:** a full librarian cycle runs end-to-end against a seeded
messy store (duplicates merged, a contradiction proposed for resolution, a
digest written); digests are preferred for broad queries in a test.

---

## Phase 6 — Bitemporal event sourcing (v0.9.0, ~3–4 days; defer if the pilot is pressing)

**Goal:** the memory becomes the business's auditable system of record, able to
answer *"what did we believe on the day we decided X?"*

- Promote the Phase-1 journal to the write path's first stop: every mutation is
  an event appended to `journal/` (monthly-sharded `events-YYYY-MM.jsonl`);
  `notes/` becomes the materialized current-state view.
- `mnemosyne replay [--as-of TIMESTAMP]` rebuilds `notes/` (or a read-only
  snapshot dir) from the journal. Round-trip test: replay(full journal) ≡
  current notes, byte-for-byte on frontmatter and body.
- `memory_recall(as_of=...)`: as-of reads against a replayed snapshot index.
  `valid_from/valid_to` (did the *fact* hold then) and journal time (did we
  *know* it then) are now cleanly distinct — bitemporal.
- Journal integrity: each event carries a hash chain (`prev_hash`) so tampering
  with history is detectable with a single pass. This completes the poisoning
  defense: nothing can be silently rewritten, only visibly appended.

**Definition of done:** replay round-trips; an as-of query returns the belief
state at a planted historical moment; hash-chain verification catches a
tampered line in a test.

---

## Phase 7 — The business pilot (v1.0.0, ongoing)

**Goal:** wire the system to the real business and run the experiment safely.
This phase is configuration, guardrails, and measurement — not new engine code.

**7.1 Roster and rings** — `AGENTS.md` in the repo, from this template:

| Writer | Runtime | Tier | Rings | Job |
|---|---|---|---|---|
| owner | human | 3 | all | final authority, policy author |
| claude-code | Claude Code | 2 | canon, team:ops | builder, reviewer, librarian duty |
| librarian | Claude Code (scheduled) | 2 | canon, proposed | curation cycle (Phase 5) |
| grok-&lt;role&gt;-N | Grok bot | 1 | private, team:&lt;role&gt;, proposed | role work (sales/support/content/…) |

Grok agents start at tier 1 deliberately: everything they learn flows through
the proposed queue before it becomes organizational truth. Promote an agent to
tier 2 only after its reputation (Phase 4) has earned it — the system gives you
the number.

**7.2 Guardrails (human gates — build these before connecting any agent to
anything that moves money or messages customers):**
- `[guardrails]` config: a list of high-stakes tags (`payment`, `contract`,
  `pricing`, `legal`, `customer-commitment`). Any agent action justified by a
  memory carrying one of these tags requires either (a) the memory is in canon
  **and** was promoted by tier ≥ 2, or (b) explicit owner approval this
  session. Enforced in the agents' protocols (their CLAUDE.md / system prompts)
  and logged via `recall_for_decision` journal events — the engine records,
  the protocol enforces, the journal proves.
- Poisoning stance, updated for a business: the v0.3.0 "caution, not
  restriction" rule stays for ordinary work, but for the high-stakes tag class
  above, suspect or contested memories **block** until a human clears them.
  This is the severe-detriment exception the original rule always carved out —
  money leaving the business qualifies.
- Secrets never enter memory (existing guardrail; now also enforced by a
  regex screen on `remember`/`update` for key-shaped and card-shaped strings —
  reject with an actionable error).

**7.3 Experiment metrics (collect from week 1, review monthly):**
- memories/week by writer and ring; proposed-queue latency (propose → verdict)
- contradiction rate and mean time-to-resolution
- % of decisions acted on that had a recorded `decision` memory (relitigation
  proxy)
- writer reputation trajectories (is any Grok agent earning tier 2?)
- recall spot-check precision (librarian samples 10 recalls/week, grades them)
- owner interventions/week at guardrail gates (should fall as policies mature)

**Definition of done (pilot exit):** one month of operation where (a) no
unreviewed memory influenced a high-stakes action, (b) the librarian cycle ran
weekly without manual repair, and (c) the metrics above are trending the right
way — at which point you have, in miniature, the v3 "institution" the ten-year
assessment described.

---

## Sequencing rationale and risk register

- **Identity before collaboration** (Phase 1 before 2–3): rings and promotion
  are meaningless if writers can be spoofed. Auth and the journal are the
  foundation everything else trusts.
- **Types before calibration** (3 before 4): you can't score outcomes without a
  `hypothesis` type to hang them on.
- **Phase 6 is the only deferrable phase.** The Phase-1 journal gives you an
  audit trail from day one; full event sourcing can land after the pilot starts
  if time pressure demands. Do not defer Phases 1–2 under any circumstances.
- **Biggest technical risk:** SQLite single-lock under many concurrent HTTP
  writers. Mitigation: the Phase-1 hammer test, and the escape hatch is already
  designed — the index is derived, so swapping it for a server-grade store
  later breaks nothing canonical.
- **Biggest operational risk:** the proposed queue silting up (review debt).
  Mitigation: queue depth in `prime` (Phase 2) and librarian duty (Phase 5).
  If nobody reviews, the system degrades to "agents have private notebooks" —
  safe, just not collaborative.
- **Biggest safety risk:** an instruction-shaped memory reaching a high-stakes
  action. Mitigation stack: tier gates on canon (P2), policy write restriction
  (P3), contested flags (P3), hash-chained journal (P6), guardrail gates (P7).

## What NOT to build (scope discipline)

No vector database, no Kafka/Redis, no Kubernetes, no agent-orchestration
framework, no web dashboard (the owner's dashboard is `prime`, `stats`, and
`git log`), and no speculative ML (learned rankers, fine-tunes). Every one of
these is a later swap the architecture already permits; none is needed to run
the pilot.
