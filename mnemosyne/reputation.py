"""Writer reputation: is this writer's confidence worth anything?

When a hypothesis (or decision) is resolved, the confidence its author held at
that moment is a forecast and the outcome is the truth: confirmed = 1,
refuted = 0, mixed = 0.5. Each resolution scores (forecast - outcome)^2, the
Brier score (0 = perfect, 0.25 = a coin flip, 1 = confidently wrong).

A writer's reputation is 1 - their mean Brier score, smoothed toward the
coin-flip baseline by a small prior so two lucky calls don't make a star:

    brier = (sum of scores + PRIOR_WEIGHT * 0.25) / (n + PRIOR_WEIGHT)
    reputation = 1 - brier          (0.75 = no track record / coin flip)

Reputation is *derived* from the resolved memories themselves (their
`resolution` frontmatter), so it can never drift from the canonical record and
survives any rebuild. It is materialized as owner-readable Markdown in
notes/canon/_writers/<writer>.md and mirrored in the index.

In recall it is a mild, bounded prior on each memory's confidence term:
multiplier = clamp(1 + (reputation - 0.75), 0.8, 1.2). It nudges, never
dominates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List

import yaml

from .schema import Memory

OUTCOMES = {"confirmed": 1.0, "refuted": 0.0, "mixed": 0.5}
BASELINE_BRIER = 0.25
PRIOR_WEIGHT = 2.0
MULT_MIN, MULT_MAX = 0.8, 1.2
NEUTRAL = 1.0 - BASELINE_BRIER


@dataclass
class Reputation:
    writer: str
    resolved: int = 0
    brier_sum: float = 0.0
    counts: Dict[str, int] = field(default_factory=lambda: {k: 0 for k in OUTCOMES})
    history: List[Dict[str, object]] = field(default_factory=list)

    @property
    def brier(self) -> float:
        return (self.brier_sum + PRIOR_WEIGHT * BASELINE_BRIER) / (self.resolved + PRIOR_WEIGHT)

    @property
    def reputation(self) -> float:
        return 1.0 - self.brier

    @property
    def multiplier(self) -> float:
        return multiplier(self.reputation)

    def to_json(self) -> Dict[str, object]:
        return {"writer": self.writer, "resolved": self.resolved,
                "brier": round(self.brier, 4), "reputation": round(self.reputation, 4),
                "multiplier": round(self.multiplier, 4), **self.counts}


def multiplier(reputation: float) -> float:
    return max(MULT_MIN, min(MULT_MAX, 1.0 + (reputation - NEUTRAL)))


def brier(forecast: float, outcome: str) -> float:
    return (float(forecast) - OUTCOMES[outcome]) ** 2


def compute(memories: Iterable[Memory]) -> Dict[str, Reputation]:
    """Fold every resolved memory into its author's reputation."""
    out: Dict[str, Reputation] = {}
    for m in memories:
        r = m.resolution
        if not r or r.get("outcome") not in OUTCOMES:
            continue
        rep = out.setdefault(m.writer, Reputation(writer=m.writer))
        score = brier(r.get("forecast", 0.5), r["outcome"])
        rep.resolved += 1
        rep.brier_sum += score
        rep.counts[r["outcome"]] += 1
        rep.history.append({"id": m.id, "outcome": r["outcome"],
                            "forecast": r.get("forecast"), "brier": round(score, 4),
                            "at": str(r.get("at", ""))[:10]})
    for rep in out.values():
        rep.history.sort(key=lambda h: (h["at"], h["id"]))
    return out


def render_markdown(rep: Reputation) -> str:
    """Deterministic (no timestamps) so an unchanged reputation never churns
    the Git-synced notes."""
    fm = yaml.safe_dump(rep.to_json(), sort_keys=False).strip()
    lines = [
        "---", fm, "---", "",
        f"Reputation of **{rep.writer}**, derived from the outcomes of the "
        "hypotheses and decisions they wrote (Brier score: 0 is perfect, 0.25 "
        "is a coin flip). Rebuilt automatically from the resolved memories; "
        "edits here are overwritten.", "",
        "| memory | outcome | forecast | brier | resolved |",
        "|---|---|---|---|---|",
    ]
    for h in rep.history[-50:]:
        lines.append(f"| {h['id']} | {h['outcome']} | {h['forecast']} | "
                     f"{h['brier']} | {h['at']} |")
    return "\n".join(lines) + "\n"
