"""Recall-quality evaluation: does the right memory come back?

A question set is JSONL, one object per line:

    {"q": "How much may the agent spend per day?", "expect": "spend cap $75 → $50"}

`expect` is a substring of the memory that answers the question. For each
question we recall `k` memories (without touching access counters) and record
the rank of the first one containing `expect`. Reported: hit@1, hit@k, mean
reciprocal rank, and the misses — run it before and after changing the
embedding model or ranking, and compare.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List


def load_questions(path: Path) -> List[Dict[str, object]]:
    rows = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            row = json.loads(line)
            if not row.get("q") or not row.get("expect"):
                raise ValueError(f"{path}:{n}: each line needs 'q' and 'expect'")
            rows.append(row)
    return rows


def evaluate(engine, questions: List[Dict[str, object]], k: int = 5,
             writer: str = None) -> Dict[str, object]:
    ranks, misses = [], []
    for row in questions:
        hits = engine.recall(str(row["q"]), k=k, entities=row.get("entities") or None,
                             record_access=False, writer=writer, digests=False)
        rank = next((i for i, h in enumerate(hits, 1) if str(row["expect"]) in h.memory.text),
                    None)
        ranks.append(rank)
        if rank is None:
            misses.append({"q": row["q"], "expect": row["expect"],
                           "got": [h.memory.text[:100] for h in hits[:2]]})
    n = len(questions) or 1
    return {"questions": len(questions), "k": k,
            "hit@1": round(sum(r == 1 for r in ranks) / n, 3),
            f"hit@{k}": round(sum(r is not None for r in ranks) / n, 3),
            "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
            "provider": engine.provider.name, "misses": misses}
