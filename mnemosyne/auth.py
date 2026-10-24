"""Writer identity and bearer-token authentication.

With more than one writer, *who wrote this* is the most important field in
the schema — so every write carries a writer id, and over the network the
writer is decided by the bearer token, never by anything the caller claims.

Tokens live only in the environment, one per writer:

    MNEMOSYNE_TOKEN_<WRITER>   e.g. MNEMOSYNE_TOKEN_GROK_SALES_1 for grok-sales-1

(the writer id upper-cased, with every non-alphanumeric character turned into
`_`). The roster of writers and their trust tiers is `[writers]` in
config.toml. A token resolves to a writer only if that writer is on the roster
and its env var holds exactly that token; anything else is rejected before any
engine call.
"""

from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from .config import Config

_WRITER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class Identity:
    writer: str
    tier: int
    teams: Tuple[str, ...] = ()


def valid_writer_id(writer: str) -> bool:
    return bool(_WRITER_RE.match(writer or ""))


def token_env_name(writer: str) -> str:
    return "MNEMOSYNE_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", writer).upper()


def identity_for(cfg: Config, writer: str) -> Identity:
    """The identity a local (stdio/CLI) caller runs as. Unknown writers get
    tier 0 — fail closed for writes."""
    spec = cfg.writers.get(writer)
    return Identity(writer=writer, tier=cfg.tier_of(writer),
                    teams=tuple(spec.teams) if spec else ())


def resolve_token(cfg: Config, token: Optional[str],
                  environ: Optional[Mapping[str, str]] = None) -> Optional[Identity]:
    """Map a bearer token to a rostered writer, or None. Constant-time compare
    against every candidate so timing does not reveal which writer matched."""
    if not token:
        return None
    env = os.environ if environ is None else environ
    match: Optional[str] = None
    for writer in cfg.writers:
        expected = env.get(token_env_name(writer))
        if expected and hmac.compare_digest(expected.encode(), token.encode()):
            match = writer
    return identity_for(cfg, match) if match else None


def bearer_token(header_value: Optional[str]) -> Optional[str]:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    if not header_value:
        return None
    scheme, _, token = header_value.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
