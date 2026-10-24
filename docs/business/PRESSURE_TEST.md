# PRESSURE_TEST.md — Mnemosyne SaaS (Oct 15, 2026)

**What this file is.** A stress test of `SAAS_PLAN.md` against the market as it
stands in October 2026: competition, demand, pricing, profitability, and
go-to-market. It does not change `SAAS_PLAN.md`; §8 lists the changes this
test recommends making to it.

**Success target used throughout:** $10–25k MRR, solo, 18–24 months after
launch, with 20+ hours a week.

---

## 0. Verdict

**Conditional go.** The business can hit the low end of the target, and
margins are excellent, but the plan as written has three problems:

1. **The wedge is narrower than `SAAS_PLAN.md` claims.** Two competitors now
   offer shared memory with audit logs for multiple agents: Anthropic (for
   Claude agents) and Basic Memory (Markdown, open source, $15/seat). "Audit
   trail" alone is now table stakes. What remains unique is vendor neutrality,
   quarantine-and-review against memory poisoning, outcome calibration, and a
   tamper-evident history.
2. **The operator buyer is real but early.** Few small businesses run agent
   teams yet. Selling directly to business owners will be slow. AI automation
   agencies that build agent teams for clients are a better first operator
   customer.
3. **$25k MRR needs the operator/agency channel, not developer volume.** A
   developer-led path to $25k needs roughly 650 Pro customers, which implies
   tens of thousands of active open-source users. That is not realistic solo
   in 24 months.

| Dimension | Score (1–5) | Why |
|---|---|---|
| Demand trend | 4 | Multi-agent and multi-vendor use rising fast; poisoning is a named, documented risk |
| Competitive intensity | 2 (crowded) | Funded leaders, a lab-native option, and a Markdown-native near-twin |
| Differentiation | 3 | Real but narrower; must be proven, not asserted |
| Pricing power | 3 | Market anchors at $19 and $125–249; governance can justify the upper band |
| Profitability | 5 | ~84–93% contribution margins with bring-your-own-key embeddings |
| Solo feasibility | 3 | Feasible if the operator side goes through agencies, not direct SMB sales |
| Time to $10k MRR | Base: month ~16 after OSS launch (month ~13 with an agency channel) | §5 model |

---

## 1. Competitive reality check

### 1.1 What changed since `SAAS_PLAN.md` was written

`SAAS_PLAN.md` §1 says none of the funded memory products leads on governance,
and describes Basic Memory as a knowledge base for one human. Both statements
are now wrong:

- **Claude Managed Agents memory** (public beta since April 23, 2026): memory
  stores can be shared across several agents with different access scopes,
  with scoped permissions and audit logs. Memories are files that can be
  exported and managed through the API. Every write becomes a session event,
  with rollback and redaction. **Limit:** it serves agents running on
  Anthropic's platform; it is not a neutral store for a Grok or GPT agent.
- **Basic Memory:** open source (AGPL-3.0), with cloud at **$15/seat/month**
  that includes shared workspaces for teams *and agents*, audit logs, and
  exportable Markdown. It also has a custom "Agent Infrastructure" tier for
  agent fleets and a partner program for people deploying it for clients.
  This is Mnemosyne's closest competitor on architecture and philosophy.
- **Zep** added Memory MCP server seats to all plans (late Aug 2026).
- **AgentHelm** launched (July 2026) as "shared memory and coordination for
  agent teams" on MCP and A2A. Tiny today, but it is the same pitch.

### 1.2 Pricing landscape (October 2026)

| Product | Entry paid | Upper self-serve | Unit | Notes |
|---|---|---|---|---|
| Mem0 | $19 Starter | $249 Pro | Add + retrieval requests | Retired its $79 Growth tier in July 2026, about two months after launching it |
| Zep | $125 Flex (50k credits) | $375 Flex Plus (200k credits) | Credits | Free tier raised to 10k credits/mo in mid-2026 |
| Letta | $20 Pro | Usage-based API | Credits | Agent runtime, not just memory |
| Supermemory | $19 Pro | $100 Max | Usage credits | Second-brain origins |
| Basic Memory | $15/seat | Custom agent infrastructure | Seats | AGPL open source; Markdown |

**What this means:**
- **$19 is the entry anchor.** Developer Pro at $19 is parity, not a selling
  point.
- **There's an accepted $100–250 band for team use** (Zep $125, Mem0 $249,
  Supermemory $100). The Operator tier belongs in that band, above the
  original $99.
- **Mem0 dropping its $79 middle tier** is a hint (not proof) that a weak
  middle tier doesn't hold; buyers sort into cheap-solo or real-team.

### 1.3 The surviving wedge

What no competitor above offers together:

1. **Vendor neutrality** — one memory for Claude, Grok, GPT, and open-weight
   agents, over MCP and HTTP. Lab-native memory can't credibly be neutral.
2. **Quarantine and review** — low-trust writers can't write shared truth
   directly; writes land in a proposed queue for a senior reviewer (ROADMAP
   Phase 2). This is a direct defense against memory poisoning.
3. **Outcome calibration** — confidence tracks whether beliefs turned out
   true, and writer reputation follows (ROADMAP Phase 4).
4. **Tamper-evident, as-of history** — "what did we believe when we decided
   X?" (ROADMAP Phase 6).

Audit logs, Markdown export, and shared stores are no longer differentiators.
Don't lead with them.

---

## 2. Demand signals

**Supporting the thesis**
- **Agents are spreading fast.** Gartner predicts 40% of enterprise apps will
  include task-specific agents by end of 2026, up from under 5% in 2025.
  Gartner also tracked a 1,445% rise in multi-agent system inquiries from Q1
  2024 to Q2 2025.
- **Governance lags.** In OutSystems' 2026 survey, only 12% of enterprises had
  a centralized platform to manage agent sprawl.
- **Multi-vendor is the norm.** In a Dataiku/Harris Poll of 600 CIOs, 81%
  expect to rely on two or more LLM providers in 2026. This is the strongest
  support for vendor neutrality.
- **Memory poisoning is a named, documented risk.** OWASP's 2026 Top 10 for
  agentic applications added Memory and Context Poisoning (ASI06) as its own
  category. Microsoft's Defender team documented 50+ memory-poisoning
  attempts from 31 companies across 14 industries in one 60-day window.
  Research presented at ICML 2026 found that existing prompt-injection
  defenses don't cover memory poisoning.

**Cautioning against it**
- **Small-business operators are early.** In a May 2026 Bluehost survey of 350
  US small-business owners, 79% knew about AI agents but only 16% used one,
  and most used AI through a single chat interface. 87% used at least one AI
  tool, but only 20% felt highly confident.
- **Many agent projects will fail.** Gartner expects more than 40% of agentic
  AI projects to be abandoned by 2027.
- **Compliance isn't a near-term lever.** The EU AI Act's Article 12 logging
  duties for high-risk systems were pushed back by the July 2026 AI omnibus.
  Tamper-evident history is good positioning, but no small customer will buy
  it for compliance in the next 12 months.

**Net:** the developer side has demand today. The operator side has demand
mainly among people who already build agent teams for others (agencies,
consultants, technical founders), not typical small-business owners.

---

## 3. Pricing — recommended changes

| Tier | `SAAS_PLAN.md` | Recommended | Reason |
|---|---|---|---|
| Open source | Free | Free (unchanged) | Distribution engine |
| Developer Free | $0 | $0 (unchanged) | Matches Zep, Mem0, Letta |
| Developer Pro | ~$19 + usage | **$19 + usage** (unchanged) | Market anchor; don't fight on price |
| Operator Workspace | ~$99 incl. 5 agents, +$15/agent | **$149 incl. 5 agents, +$19/agent** | Sits in the accepted $100–250 team band; governance justifies it |
| **Agency (new)** | — | **$399 for up to 10 client workspaces** | Matches the best operator channel (§6); Basic Memory's partner program shows the demand |
| Enterprise | Custom | Custom, from ~$1,000/mo | Self-hosted sync, SSO |

**Pricing unit:** charge Operator and Agency by **governed agent writer**,
not by request volume. That is the value unit (more agents = more risk to
govern) and it separates Mnemosyne from the request-quota pricing of Mem0 and
Zep.

**Validate before publishing:** ask 10 developers and 10 agency or operator
prospects what they'd pay. Treat every number above as a hypothesis.

---

## 4. Profitability

### 4.1 Unit economics (assumptions in Appendix A)

| Tier | Assumed ARPU | Contribution after Stripe + infra | Margin |
|---|---|---|---|
| Developer Pro | $24 (incl. modest overage) | $22.25 | 92.7% |
| Operator | $187 ($149 + 2 extra agents) | $172.28 | 92.1% |
| Agency | $399 | $333.13 | 83.5% |
| Enterprise | $1,000 | $910.70 | 91.1% |

Fixed costs assumed at **$620/month** (hosting baseline, tooling, build
tooling, bookkeeping/legal reserve). Margins depend on **bring-your-own-key
embeddings** and the cost of a weekly server-side librarian run. If you
include embeddings in the price, re-run the model.

### 4.2 What it takes to hit the target

| Mix | $10k MRR | $25k MRR |
|---|---|---|
| **Operator-led** | 53 Pro, 31 Operator, 5 Agency, 1 Enterprise → profit ≈ $8.5k/mo | 132 Pro, 77 Operator, 13 Agency, 2 Enterprise → profit ≈ $21.7k/mo |
| **Developer-led** | 260 Pro, 16 Operator, 2 Agency → profit ≈ $8.6k/mo | 651 Pro, 39 Operator, 5 Agency → profit ≈ $22.3k/mo |

"Profit" here is before taxes and before paying yourself; founder time is not
costed.

**The developer-led $25k path doesn't work solo.** At a 1–3% free-to-paid
conversion (a common rule of thumb for developer tools), 651 Pro customers
need roughly 22,000–65,000 active open-source users. The operator-led path
needs about 4,400–13,200 for its 132 Pro customers, and most revenue comes
from about 90 higher-value accounts.

---

## 5. Growth scenarios (months after OSS launch)

Assumptions: Developer Cloud opens month 2, Operator beta month 5, enterprise
deals from month 9; new-customer rates ramp over six months; monthly churn
3–5%. Details in Appendix A.

| Scenario | Month 12 MRR | Month 18 MRR | Month 24 MRR | $10k reached | $25k reached |
|---|---|---|---|---|---|
| Bear | $2.8k | $4.7k | $6.1k | No | No |
| Base | $6.9k | $12.8k | $17.8k | Month 16 | No |
| Bull | $12.3k | $23.0k | $32.2k | Month 11 | Month 20 |

**Sensitivity — what moves the result:**
- **Channel beats price.** Base case with an agency channel (operator signups
  1.5×, agency signups 2×): **$10k at month 13, ~$24.6k at month 24.**
- **Original $99 Operator price:** base reaches $10k a month later (month 17)
  and ends at $15.1k instead of $17.8k.

**Clock start:** these months count from the open-source launch, which needs
ROADMAP Phases 0–2 plus docs and quickstarts. At 20+ hours a week, expect
roughly 4–8 weeks to get there (estimate).

---

## 6. Market strategy

### 6.1 Reposition
**From:** "shared, auditable memory for teams of AI agents."
**To:** "**the neutral memory layer for mixed-model agent teams** — every
write has an author, low-trust agents can't poison shared truth, and you own
the Markdown."

Lead with **poisoning resistance + vendor neutrality**. Audit logs and export
appear as proof points, not the headline.

### 6.2 Integrate with the competition instead of fighting it
- **Importers** from Claude Managed Agents memory stores and Basic Memory
  folders (both are file-based). This makes Mnemosyne the layer *above*
  single-vendor memory rather than a replacement.
- **Framework adapters** (CrewAI, LangGraph, and the Grok bot setup you
  already run) so developers meet it where they build.

### 6.3 Pick the operator beachhead deliberately
1. **First: AI automation agencies and consultants** who build agent teams for
   clients. They already run mixed vendors, manage several clients (one
   workspace each), and carry the blame when an agent misbehaves.
2. **Second: technical founders running their own agent teams** (the pilot
   profile).
3. **Later: non-technical small-business owners**, once the dashboard and
   templates make it turnkey.

### 6.4 Prove the wedge with evidence, not claims
- **A public poisoning drill:** a reproducible demo where a low-trust agent
  writes a poisoned memory, it lands in quarantine, a contradiction is
  flagged, and nothing reaches shared truth. Publish the script.
- **Benchmarks:** run LongMemEval for recall quality, plus a poisoning
  benchmark (such as the MPBench work presented at ICML 2026, if its code is
  available) for the governance claim. Publish methods and scripts.
- **The anonymous case study** (`SAAS_PLAN.md` §8) after ≥ 4 weeks of pilot.

---

## 7. Go-to-market plan

### 7.1 Before launch (now → M1, ~4–8 weeks)
- **Decisions:** license, name and trademark search (`SAAS_PLAN.md` §9).
- **20 validation conversations:** 10 developers who build multi-agent
  systems, 10 agencies or operators. Ask what they use for shared memory
  today, whether a poisoned or wrong shared memory has burned them, and what
  they'd pay.
- **Landing page + waitlist**, with the poisoning-drill demo as the hero.
- **Build in public:** the daily X posts start covering Mnemosyne's
  build toward launch.

### 7.2 Launch (M1 — open source)
- Show HN; relevant subreddits; MCP server directories and registries;
  "awesome MCP" lists.
- Pitch the authors of "best MCP memory server" roundups (several published in
  2026) with the neutral-and-poisoning-resistant angle.
- Two quickstarts: five-minute single agent; ten-minute "two agents from two
  vendors, one quarantined write."

### 7.3 Monetize developers (M2 — Developer Cloud)
- Free and Pro tiers; usage limits that block writes but never reads.
- Framework adapters and their integration listings.

### 7.4 Monetize operators (M3 — Operator + Agency beta)
- **3–5 agency design partners**, free during beta in exchange for weekly
  feedback and an anonymized case study each.
- Ready-made agent rosters (sales + support + content) that start every agent
  at tier 1.
- The agency plan launches with these partners as references.

### 7.5 Weekly time split at 20+ hours
| Phase | Build | Distribution/content | Customer conversations |
|---|---|---|---|
| Pre-launch → M1 | 60% | 20% | 20% |
| M1 → M3 | 50% | 30% | 20% |
| M3 → GA | 40% | 30% | 30% |

---

## 8. Recommended changes to `SAAS_PLAN.md`

Not applied yet; these await your go-ahead.

1. **§1 positioning:** remove "none of them leads on governance"; correct the
   Basic Memory description; add Claude Managed Agents memory; reposition per
   §6.1.
2. **§2 rule check, rule 1:** governance is contested; the wedge is neutrality
   + poisoning resistance + calibration + as-of history.
3. **§3 tiers:** Operator to $149 incl. 5 agents (+$19/agent); add the Agency
   plan; price by governed agent writer.
4. **§3 sequencing:** the operator side starts with agencies, not small
   business owners.
5. **§5/§6:** add importers (Claude memory stores, Basic Memory) and the
   public poisoning drill to the cloud build plan.
6. **§10 kill criteria — add:** reassess if Anthropic opens Managed Agents
   memory to non-Claude agents, or if Basic Memory adds reviewed promotion
   for agent writers.
7. **§11 risks — add:** "operator market too early" (mitigation: agencies
   first).

---

## 9. Idea-rule check (re-run with October 2026 facts)

1. **Already done?** Partly. Shared agent memory with audit logs exists
   (Anthropic, Basic Memory). Vendor-neutral, poisoning-resistant,
   calibrated memory for mixed teams does not. Entry is still possible on
   that narrower wedge. **Pass, narrowly.**
2. **80% by prompting an agent?** No. **Pass.**
3. **Sellable today?** Developer tier yes; operator tier through agencies.
   **Pass, with the channel change.**
4. **Valuable in 5–10 years?** Yes, as multi-vendor fleets grow; the risk is a
   lab making its memory neutral. **Pass, with a watch item.**
5. **Solo wedge?** Yes for $10k MRR; $25k needs the agency channel to work.
   **Pass, conditional.**

---

## Appendix A — Model assumptions

Model script: `docs/business/pressure_test_model.py`.

- **ARPU:** Pro $24; Operator $149 + 2 extra agents × $19 = $187; Agency $399;
  Enterprise $1,000.
- **Variable cost:** Stripe 2.9% + $0.30; infra Pro $0.75, Operator $3 + $6
  librarian LLM run, Agency $54 (≈6 active client workspaces), Enterprise $60.
  Embeddings are bring-your-own-key.
- **Fixed cost:** $620/month.
- **Growth:** new-customer rates ramp linearly over six months from each
  tier's opening month. Base: 12 Pro, 3.5 Operator, 0.4 Agency per month at
  full ramp; one enterprise deal every six months from month 9; churn 4%
  (Pro) and 3.5% (Operator). Bear and bull vary these (see script).
- **Not included:** founder pay, taxes, SOC 2 or other audits, paid
  advertising.

## Sources

- Mem0 pricing: https://www.mem0.ai/pricing ; Growth tier retired:
  https://usagepricing.com/blueprint/activity/mem0-2026-07-21-packaging
- Zep pricing changes: https://pricingsaas.com/companies/getzep/diffs/2026Q2
- Letta plans: https://docs.letta.com/guides/api/plans
- Supermemory pricing: https://toolradar.com/tools/supermemory/pricing
- Basic Memory pricing: https://basicmemory.com/pricing
- Claude Managed Agents memory: https://claude.com/blog/claude-managed-agents-memory ;
  https://sdtimes.com/anthropic/anthropic-adds-memory-to-claude-managed-agents/
- AgentHelm: https://www.producthunt.com/products/agenthelm/launches
- Gartner forecasts (via): https://www.workclaw.com/blog/ai-agent-trends-2026
- OutSystems 2026 survey: https://itwire.com/guest-articles/guest-research/agentic-ai-goes-mainstream-in-the-enterprise,-but-94-raise-concern-about-sprawl,-outsystems-research-finds.html
- Multi-vendor CIO survey: https://www.dataiku.com/blog/best-llms-for-business
- OWASP ASI06 and agent incidents: https://levelup.gitconnected.com/the-ai-agent-security-problem-stopped-being-hypothetical-in-2026-c0b81c438d72 ;
  https://www.morphllm.com/ai-coding-agent-security
- Memory poisoning study (ICML 2026): https://icml.cc/virtual/2026/67964
- Small-business AI survey: https://www.bluehost.com/state-of-small-business-ai-confidence
- EU AI Act omnibus delay: https://complexdiscovery.com/when-the-agent-becomes-a-witness-an-oxford-style-tutorial-on-ai-evidence-accountability-and-discovery/
- Open-source conversion rule of thumb: https://www.getmonetizely.com/articles/whats-the-relationship-between-open-source-adoption-and-saas-revenue
