"""Mnemosyne SaaS profitability model. All inputs are assumptions, stated here."""

# ---- Pricing (revised hypotheses) ----
PRO_PRICE = 19.0            # Developer Pro list
PRO_ARPU = 24.0             # incl. modest overage
OP_BASE = 149.0             # Operator workspace, incl. 5 agents
OP_EXTRA_AGENT = 19.0
OP_AVG_EXTRA_AGENTS = 2.0
OP_ARPU = OP_BASE + OP_EXTRA_AGENT * OP_AVG_EXTRA_AGENTS
AGENCY_PRICE = 399.0        # agency plan, up to 10 client workspaces
ENT_ARPU = 1000.0

# ---- Variable costs per paying account per month ----
STRIPE_PCT, STRIPE_FIXED = 0.029, 0.30
INFRA_PER_PRO = 0.75        # storage + compute share, BYOK embeddings
INFRA_PER_OP = 3.00         # managed index + backups
LLM_LIBRARIAN_PER_OP = 6.00 # weekly server-side librarian run (Managed mode)
INFRA_PER_AGENCY = 3.00 * 6 + 6.00 * 6  # ~6 active client workspaces
INFRA_PER_ENT = 60.0

# ---- Fixed monthly costs ----
FIXED = {
    "hosting baseline": 150,
    "tooling (email, analytics, status, domain)": 120,
    "Claude/agent build tooling": 200,
    "bookkeeping/legal reserve": 150,
}
FIXED_TOTAL = sum(FIXED.values())


def net(price, infra):
    return price - (price * STRIPE_PCT + STRIPE_FIXED) - infra


print(f"Operator ARPU: ${OP_ARPU:.0f}")
for name, p, c in [("Pro", PRO_ARPU, INFRA_PER_PRO),
                   ("Operator", OP_ARPU, INFRA_PER_OP + LLM_LIBRARIAN_PER_OP),
                   ("Agency", AGENCY_PRICE, INFRA_PER_AGENCY),
                   ("Enterprise", ENT_ARPU, INFRA_PER_ENT)]:
    n = net(p, c)
    print(f"{name:10s} price ${p:7.0f}  contribution ${n:7.2f}  margin {n/p:5.1%}")
print(f"Fixed costs/month: ${FIXED_TOTAL}")

# ---- Customer counts needed for target MRR (mix A: operator-led) ----
print("\nCustomer mixes that hit each target:")
mixes = {
    "Operator-led": dict(pro=60, op=35, agency=6, ent=1),
    "Developer-led": dict(pro=250, op=15, agency=2, ent=0),
}
targets = {"$10k": 10_000, "$25k": 25_000}
for mname, mix in mixes.items():
    base = (mix["pro"] * PRO_ARPU + mix["op"] * OP_ARPU
            + mix["agency"] * AGENCY_PRICE + mix["ent"] * ENT_ARPU)
    for tname, t in targets.items():
        k = t / base
        counts = {k2: round(v * k) for k2, v in mix.items()}
        mrr = (counts["pro"] * PRO_ARPU + counts["op"] * OP_ARPU
               + counts["agency"] * AGENCY_PRICE + counts["ent"] * ENT_ARPU)
        contrib = (counts["pro"] * net(PRO_ARPU, INFRA_PER_PRO)
                   + counts["op"] * net(OP_ARPU, INFRA_PER_OP + LLM_LIBRARIAN_PER_OP)
                   + counts["agency"] * net(AGENCY_PRICE, INFRA_PER_AGENCY)
                   + counts["ent"] * net(ENT_ARPU, INFRA_PER_ENT))
        profit = contrib - FIXED_TOTAL
        print(f"{mname:13s} {tname}: {counts}  MRR ${mrr:,.0f}  "
              f"profit ${profit:,.0f}/mo ({profit/mrr:.0%})")

# ---- OSS funnel: active users needed for Pro counts ----
print("\nActive OSS users needed for Pro customers (conversion 1% / 3%):")
for pro in (60, 150, 250):
    print(f"  {pro} Pro -> {pro/0.03:,.0f} to {pro/0.01:,.0f} active users")

# ---- Month-by-month growth, 24 months from OSS launch ----
# new paying accounts per month by phase; churn monthly
def simulate(name, pro_new, op_new, agency_new, ent_new, churn_pro, churn_op):
    pro = op = ag = ent = 0.0
    hit10 = hit25 = None
    for m in range(1, 25):
        # cloud dev tier opens month 2, operator beta month 5, GA month 7
        if m >= 2:
            pro = pro * (1 - churn_pro) + pro_new(m)
        if m >= 5:
            op = op * (1 - churn_op) + op_new(m)
            ag = ag * (1 - churn_op / 2) + agency_new(m)
        if m >= 9:
            ent = ent + ent_new(m)
        mrr = pro * PRO_ARPU + op * OP_ARPU + ag * AGENCY_PRICE + ent * ENT_ARPU
        if hit10 is None and mrr >= 10_000:
            hit10 = m
        if hit25 is None and mrr >= 25_000:
            hit25 = m
        if m in (6, 12, 18, 24):
            print(f"  {name:5s} month {m:2d}: Pro {pro:5.0f}  Op {op:4.0f}  "
                  f"Agency {ag:3.0f}  Ent {ent:3.0f}  MRR ${mrr:8,.0f}")
    print(f"  {name:5s} -> $10k MRR at month {hit10}, $25k at month {hit25}")


print("\nGrowth scenarios (months from OSS launch):")
ramp = lambda m, start, per: per * min(1.0, max(0, m - start + 1) / 6)
simulate("Bear", lambda m: ramp(m, 2, 6), lambda m: ramp(m, 5, 1.5),
         lambda m: 0.15, lambda m: 0.0, 0.05, 0.05)
simulate("Base", lambda m: ramp(m, 2, 12), lambda m: ramp(m, 5, 3.5),
         lambda m: ramp(m, 5, 0.4), lambda m: 1/6, 0.04, 0.035)
simulate("Bull", lambda m: ramp(m, 2, 22), lambda m: ramp(m, 5, 6),
         lambda m: ramp(m, 5, 0.8), lambda m: 1/4, 0.035, 0.03)
