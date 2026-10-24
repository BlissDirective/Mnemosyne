# PILOT.md: running the business pilot

**Goal.** Wire Mnemosyne to the real business and run the experiment safely.
This phase is configuration, guardrails, and measurement. The engine is
done.

## Before connecting any agent to money or customers

- [ ] `config.toml` roster and `[guardrails]` filled in ([`AGENTS.md`](../AGENTS.md) §2)
- [ ] one token per writer, stored only in the server's environment
- [ ] one `mnemosyne-server --http` (and, if needed, `mnemosyne-gateway`) process, on localhost or behind TLS
- [ ] every agent's system prompt carries the protocol in [`AGENTS.md`](../AGENTS.md) §3, including rule 6 (the decision gate)
- [ ] the owner has written the first policies (`--type policy --ring canon`): discount floors, refund limits, brand voice
- [ ] the librarian is scheduled weekly ([`LIBRARIAN.md`](../LIBRARIAN.md))
- [ ] `notes/`, `archive/`, and `journal/` are committed to a private Git repo daily; the `verify-journal` head hash goes in the commit message
- [ ] `mnemosyne verify-journal` passes

## Weekly (owner, about 20 minutes)

1. Read this week's digest (`mnemosyne recall "this week" -k 10`; the digest comes first).
2. Work whatever the librarian left for you: proposed resolutions, policy proposals, and anything touching money.
3. `mnemosyne metrics --since <last week>`. Look at the queue latency and the blocked checks.

## Monthly review: the metrics

| Metric | Healthy trend |
|---|---|
| new memories per week, by writer and ring | steady; canon growing slower than private (review is a filter) |
| proposal → verdict latency | median under 7 days; nothing open more than 14 |
| contradictions: open vs. resolved, time to resolution | open count flat or falling |
| decision coverage (checks that cited a recorded decision) | rising; low means decisions are being relitigated |
| reputation trajectories | who is earning tier 2? |
| recall spot-check precision (librarian, 10 per week) | 7/10 or better and stable |
| owner interventions per week at the gate | falling as policies mature |

## Exit criteria (one month of operation)

- **(a)** No unreviewed memory influenced a high-stakes action. Every
  `recall_for_decision` event with decision `clear` cites only `clear`,
  `cleared`, or `advisory` memories. The journal proves it, and
  `tests/test_pilot.py` shows the check.
- **(b)** The librarian cycle ran weekly without manual repair. There is one
  digest per ISO week (`digest:<week>` tags).
- **(c)** The metrics above trend the right way.

When all three hold, you have the "institution" the roadmap describes, in
miniature: memory with authorship, review, calibration, and an auditable
history.

## Known limits

- **Single writer process per store.** The SQLite index uses one lock, and
  the journal chain must stay linear. The index is derived, so a server-grade
  store can replace it later without touching anything canonical.
- **The decision gate is enforced by protocol.** The engine records and
  judges; the agent must call it. The journal shows whether every high-stakes
  action had a check, so audit for gaps monthly.
- **Truncation of the journal's tail** is detectable only against a recorded
  head hash. Keep committing it.
