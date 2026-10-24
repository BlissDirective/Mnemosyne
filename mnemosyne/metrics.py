"""Pilot metrics (ROADMAP Phase 7.3), computed from the journal and notes.

Collect from week 1, review monthly:

  memories_per_week          new memories by ISO week, writer, and ring
  proposal_latency           proposal -> verdict (promote/reject), in hours
  contradictions             how many were recorded, how many are resolved,
                             mean time to resolution
  decision_coverage          share of recall_for_decision calls that cited a
                             recorded `decision` memory (relitigation proxy)
  reputation_trajectories    each writer's reputation after every resolution
  recall_spot_checks         the librarian's weekly graded samples
                             (memories tagged spotcheck:<week>, "8/10 ...")
  owner_interventions        per week: owner approvals at the gate, and
                             blocked decision checks

Everything is derived from canonical data, so the numbers can be recomputed
for any past period at any time.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime
from typing import Any, Dict, List, Optional

from .reputation import BASELINE_BRIER, OUTCOMES, PRIOR_WEIGHT
from .schema import utcnow_iso

_NEW_OPS = {"remember", "propose", "external"}


def _week(ts: str) -> str:
    y, w, _ = datetime.fromisoformat(ts).isocalendar()
    return f"{y}-W{w:02d}"


def _hours(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600


def _summary(xs: List[float]) -> Dict[str, Optional[float]]:
    if not xs:
        return {"n": 0, "mean": None, "median": None, "p90": None}
    xs = sorted(xs)
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 2),
            "median": round(statistics.median(xs), 2),
            "p90": round(xs[min(len(xs) - 1, int(0.9 * len(xs)))], 2)}


def compute(eng, since: Optional[str] = None) -> Dict[str, Any]:
    events = [e for e in eng.journal.events() if not since or e["ts"] >= since]

    # memories created, by week x writer x ring
    per_week: Dict[str, Dict[str, int]] = {}
    seen = set()
    for e in events:
        if e["op"] in _NEW_OPS and e.get("memory_id") not in seen:
            seen.add(e.get("memory_id"))
            ring = (e.get("state") or {}).get("ring", "canon")
            key = f"{e['writer']} @ {ring}"
            wk = per_week.setdefault(_week(e["ts"]), {})
            wk[key] = wk.get(key, 0) + 1

    # proposal latency: entered the queue -> promoted / rejected
    entered: Dict[str, str] = {}
    latencies: List[float] = []
    for e in events:
        st = e.get("state") or {}
        mid = e.get("memory_id")
        if st.get("ring") == "proposed" and e["op"] in ("remember", "propose") and mid:
            entered[mid] = e["ts"]
        elif e["op"] in ("promote", "reject") and mid in entered:
            latencies.append(_hours(entered.pop(mid), e["ts"]))
    now = utcnow_iso()
    open_ages = [_hours(ts, now) for ts in entered.values()]

    # contradictions: recorded contradicts edges, resolved when either side
    # stops being live (superseded by a live edge, retired, refuted, deleted)
    pairs: Dict[frozenset, Dict[str, Any]] = {}
    for e in events:
        st = e.get("state") or {}
        if e["op"] == "relate" and st.get("ring") != "proposed":
            for r in st.get("relations", []):
                if r.get("rel") == "contradicts":
                    pairs.setdefault(frozenset((e["memory_id"], r["id"])),
                                     {"opened": e["ts"], "resolved": None})
        retired_ids = set()
        if st.get("ring") not in (None, "proposed"):
            retired_ids |= {r["id"] for r in st.get("relations", [])
                            if r.get("rel") == "supersedes"}
        if st.get("duplicate_of") or (st.get("resolution") or {}).get("outcome") == "refuted":
            retired_ids.add(e.get("memory_id"))
        if e["op"] in ("forget", "external_delete"):
            retired_ids.add(e.get("memory_id"))
        for pair, info in pairs.items():
            if info["resolved"] is None and pair & retired_ids and e["ts"] >= info["opened"]:
                info["resolved"] = e["ts"]
    ttr = [_hours(i["opened"], i["resolved"]) for i in pairs.values() if i["resolved"]]

    # decision coverage (relitigation proxy)
    checks = [e for e in events if e["op"] == "recall_for_decision"]
    with_decision = sum(1 for e in checks
                        if any(m.get("type") == "decision" for m in e.get("memories", [])))

    # reputation trajectories: replay resolutions in order
    traj: Dict[str, List[Dict[str, Any]]] = {}
    acc: Dict[str, List[float]] = {}
    for e in events:
        st = e.get("state") or {}
        res = st.get("resolution") or {}
        if e["op"] == "resolve" and res.get("outcome") in OUTCOMES:
            w = st.get("writer", "?")
            s = acc.setdefault(w, [0.0, 0])
            s[0] += (float(res.get("forecast", 0.5)) - OUTCOMES[res["outcome"]]) ** 2
            s[1] += 1
            rep = 1 - (s[0] + PRIOR_WEIGHT * BASELINE_BRIER) / (s[1] + PRIOR_WEIGHT)
            traj.setdefault(w, []).append({"ts": e["ts"], "outcome": res["outcome"],
                                           "reputation": round(rep, 4)})

    # librarian spot checks: "… 8/10 …" in memories tagged spotcheck:<week>
    spot = []
    for m in eng.store.iter_memories():
        tag = next((t for t in m.tags if t.startswith("spotcheck:")), None)
        hit = re.search(r"(\d+)\s*/\s*(\d+)", m.text) if tag else None
        if hit and int(hit.group(2)):
            spot.append({"week": tag.split(":", 1)[1], "useful": int(hit.group(1)),
                         "sampled": int(hit.group(2)),
                         "precision": round(int(hit.group(1)) / int(hit.group(2)), 3)})
    spot.sort(key=lambda s: s["week"])

    # owner interventions at the gate
    interventions: Dict[str, Dict[str, int]] = {}
    for e in events:
        if e["op"] == "approve" or (e["op"] == "recall_for_decision"
                                    and e.get("decision") == "blocked"):
            wk = interventions.setdefault(_week(e["ts"]), {"approvals": 0, "blocked_checks": 0})
            wk["approvals" if e["op"] == "approve" else "blocked_checks"] += 1

    return {
        "since": since,
        "events": len(events),
        "memories_per_week": dict(sorted(per_week.items())),
        "proposal_latency_hours": {**_summary(latencies), "open": len(entered),
                                   "oldest_open_hours": round(max(open_ages), 1)
                                   if open_ages else None},
        "contradictions": {"recorded": len(pairs), "resolved": len(ttr),
                           "open": len(pairs) - len(ttr),
                           "time_to_resolution_hours": _summary(ttr)},
        "decision_coverage": {"checks": len(checks), "cited_a_decision": with_decision,
                              "share": round(with_decision / len(checks), 3)
                              if checks else None},
        "reputation_trajectories": traj,
        "recall_spot_checks": spot,
        "owner_interventions": dict(sorted(interventions.items())),
    }


def _h(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x}h"


def render(m: Dict[str, Any]) -> str:
    lat, con, cov = m["proposal_latency_hours"], m["contradictions"], m["decision_coverage"]
    lines = [f"# Mnemosyne pilot metrics ({m['events']} journal events"
             + (f" since {m['since']}" if m["since"] else "") + ")", "",
             "## New memories per week (writer @ ring)"]
    for wk, rows in m["memories_per_week"].items():
        lines.append(f"- {wk}: " + ", ".join(f"{k} {v}" for k, v in sorted(rows.items())))
    lines += ["", "## Review queue",
              f"- proposal -> verdict: n={lat['n']} median={_h(lat['median'])} "
              f"p90={_h(lat['p90'])}; open={lat['open']} "
              f"(oldest {_h(lat['oldest_open_hours'])})",
              "", "## Contradictions",
              f"- recorded {con['recorded']}, resolved {con['resolved']}, open {con['open']}; "
              f"mean time to resolution {_h(con['time_to_resolution_hours']['mean'])}",
              "", "## Decision coverage (relitigation proxy)",
              f"- {cov['cited_a_decision']}/{cov['checks']} decision checks cited a recorded "
              f"decision ({'n/a' if cov['share'] is None else cov['share']})",
              "", "## Reputation trajectories"]
    for w, pts in sorted(m["reputation_trajectories"].items()):
        lines.append(f"- {w}: " + " -> ".join(f"{p['reputation']:.2f}" for p in pts))
    lines += ["", "## Recall spot checks"]
    lines += [f"- {s['week']}: {s['useful']}/{s['sampled']}" for s in m["recall_spot_checks"]] \
        or ["- (none recorded yet; see LIBRARIAN.md step 5)"]
    lines += ["", "## Owner interventions at guardrail gates"]
    lines += [f"- {wk}: {v['approvals']} approvals, {v['blocked_checks']} blocked checks"
              for wk, v in m["owner_interventions"].items()] or ["- none"]
    return "\n".join(lines)
