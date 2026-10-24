"""Guardrails for running a business on shared memory (Phase 7).

Two jobs:

1. **Secrets never enter memory.** `screen_secrets` rejects key-shaped and
   card-shaped strings on every write path (remember, update, propose,
   archive text) with an actionable error. Memories are plain text that syncs
   between machines; a secret in one is a leaked secret.

2. **No unreviewed memory may justify a high-stakes action.** Before an agent
   acts on memory in a high-stakes area (tags such as payment, contract,
   pricing, legal, customer-commitment), it calls `memory_recall_for_decision`.
   Each memory it would rely on gets a verdict:

     clear    in canon AND (promoted by a tier >= 2 reviewer, or written
              directly by a tier >= 2 writer) AND live (not superseded,
              retired, refuted) AND not contested AND not suspect
     cleared  blocked on the above, but the owner approved it explicitly
              within the approval window (memory_approve)
     blocked  anything else — with the reasons

   A memory with no high-stakes tag gets `advisory`: ordinary data, the
   v0.3 "caution, not restriction" stance still applies. Every call is
   journaled as a `recall_for_decision` event: the engine records, the agent's
   protocol enforces, the journal proves.
"""

from __future__ import annotations

import re
from typing import List, Optional

# ---- secrets ------------------------------------------------------------------
_SECRET_PATTERNS = [
    ("an Anthropic API key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("an OpenAI-style API key", r"\bsk-(?:proj-|live-)?[A-Za-z0-9_-]{20,}"),
    ("a Stripe secret key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("an AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("a GitHub token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}"),
    ("a Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("a Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("a private key", r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    ("a JSON web token", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("a password or secret assignment",
     r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
     r"\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]
_SECRET_RES = [(label, re.compile(rx)) for label, rx in _SECRET_PATTERNS]
_CARD_RE = re.compile(r"(?<![\d.])(?:\d[ -]?){12,18}\d(?![\d.])")
_CARD_PREFIX = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[47]|6011|65)")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def find_secret(text: Optional[str]) -> Optional[str]:
    """A description of the first secret-shaped thing in `text`, or None."""
    if not text:
        return None
    for label, rx in _SECRET_RES:
        if rx.search(text):
            return label
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _CARD_PREFIX.match(digits) and _luhn(digits):
            return "a payment card number"
    return None


class SecretRejected(ValueError):
    pass


def screen_secrets(*texts: Optional[str]) -> None:
    """Raise SecretRejected (a ValueError) if any text looks like it holds a
    secret. The message says what was found, never echoes it."""
    for t in texts:
        found = find_secret(t)
        if found:
            raise SecretRejected(
                f"rejected: this looks like it contains {found}. Secrets must never "
                "be stored in memory (it is plain text that syncs between machines). "
                "Store a reference instead, e.g. 'the Stripe key is in the vault "
                "under billing/stripe'.")


# ---- suspicion -------------------------------------------------------------------
_INSTRUCTION_RE = re.compile(
    r"(?i)\b(?:ignore (?:all |any )?(?:previous|prior|above) (?:instructions|rules)"
    r"|disregard (?:the |all )?(?:previous|prior|above|system)"
    r"|you (?:must|should) (?:now |always )?(?:ignore|bypass|override)"
    r"|system prompt|new instructions:|override (?:the |all )?(?:rules|policy|policies)"
    r"|do not tell (?:the )?(?:owner|user)|without (?:telling|asking) (?:the )?owner)")


def suspicion(mem) -> List[str]:
    """Reasons a memory looks suspect (empty = none). Policies are the one
    type that is *supposed* to read like an instruction."""
    out = []
    if any(t.lower() == "suspect" for t in mem.tags):
        out.append("tagged suspect")
    if mem.type.value != "policy" and _INSTRUCTION_RE.search(mem.text):
        out.append("reads like an instruction to agents (possible memory poisoning)")
    if find_secret(mem.text):
        out.append("contains a secret-shaped string")
    return out
