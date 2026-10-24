"""Embedding providers.

The provider is deliberately swappable and every stored vector records which
model produced it. That is what makes the system "evolutionary": when a better
model ships, point the config at it and run `reindex` — the canonical text is
preserved, so re-embedding is a rebuild, not a data-loss event.

Providers:
  DevEmbedding     deterministic, offline, zero-setup (for testing / air-gapped).
  OpenAIEmbedding  text-embedding-3-large by default (MRL: `dim` can truncate).
  VoyageEmbedding  voyage-3-large by default (retrieval-optimized, asymmetric:
                   documents and queries are embedded with different input_type).

HTTP providers batch their requests (BATCH_SIZE texts per call) and retry
transient failures (429 / 5xx / network) with exponential backoff, so a full
`reindex --force` over hundreds of memories is a routine operation, not a
single giant request that hits provider limits.

A local option (e.g. BGE-M3 / Qwen3-Embedding via sentence-transformers) is a
drop-in: subclass EmbeddingProvider, implement `_embed`, register in `get_provider`.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import List, Optional

import numpy as np


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


class EmbeddingProvider:
    name: str = "base"
    dim: int = 0

    def embed(self, texts: List[str]) -> np.ndarray:
        """Return an (n, dim) float32 matrix of L2-normalized *document* embeddings."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return _l2_normalize(self._embed(texts))

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a *query* (retrieval-time). Symmetric providers just reuse the
        document path; asymmetric providers (Voyage) override this so queries
        get input_type='query' — mixing the two measurably hurts retrieval."""
        return self.embed_one(text)

    def _embed(self, texts: List[str]) -> np.ndarray:  # pragma: no cover - abstract
        raise NotImplementedError


class DevEmbedding(EmbeddingProvider):
    """Deterministic hashing embedding. Not competitive with real models, but it
    captures lexical overlap well enough to exercise the full pipeline offline,
    and it needs no API key and no network."""

    def __init__(self, dim: int = 256):
        self.name = f"dev-hash-{dim}"
        self.dim = dim

    def _embed(self, texts: List[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in _tokenize(t):
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
                idx = h % self.dim
                sign = 1.0 if (h >> 8) & 1 else -1.0
                out[i, idx] += sign
        return out


def _tokenize(text: str) -> List[str]:
    return [w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split() if w]


class _HTTPEmbedding(EmbeddingProvider):
    """Shared plumbing for HTTP embedding APIs (kept dependency-light via httpx).

    Handles batching and retries; subclasses implement `_request(texts, is_query)`
    to make one API call for one chunk."""

    endpoint: str = ""
    api_key_env: str = ""
    BATCH_SIZE: int = 100          # texts per request — safely inside provider limits
    MAX_RETRIES: int = 5
    BACKOFF_BASE: float = 1.5      # seconds; grows exponentially per attempt

    def __init__(self, model: str, dim: int, api_key: Optional[str] = None):
        self.name = model
        self.dim = dim
        self._api_key = api_key or os.environ.get(self.api_key_env, "")

    def _headers(self) -> dict:
        if not self._api_key:
            raise RuntimeError(
                f"Missing API key: set ${self.api_key_env} to use provider '{self.name}'. "
                f"(Or use provider='dev' for offline testing.)"
            )
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    # ---- batching + retry ------------------------------------------------
    def _embed(self, texts: List[str]) -> np.ndarray:
        return self._embed_batched(texts, is_query=False)

    def embed_query(self, text: str) -> np.ndarray:
        return _l2_normalize(self._embed_batched([text], is_query=True))[0]

    def _embed_batched(self, texts: List[str], is_query: bool) -> np.ndarray:
        chunks = [texts[i:i + self.BATCH_SIZE] for i in range(0, len(texts), self.BATCH_SIZE)]
        parts = [self._request_with_retry(chunk, is_query) for chunk in chunks]
        return np.vstack(parts) if parts else np.zeros((0, self.dim), dtype=np.float32)

    def _request_with_retry(self, texts: List[str], is_query: bool) -> np.ndarray:
        import httpx
        last_err: Optional[Exception] = None
        for attempt in range(self.MAX_RETRIES):
            try:
                return self._request(texts, is_query)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 429 or status >= 500:
                    last_err = e            # transient — back off and retry
                else:
                    raise                   # 4xx (bad key, bad input) — fail fast
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_err = e
            time.sleep(self.BACKOFF_BASE * (2 ** attempt))
        raise RuntimeError(
            f"Embedding request failed after {self.MAX_RETRIES} retries "
            f"({type(last_err).__name__}: {last_err}). "
            f"Check network/rate limits, or use provider='dev' offline."
        )

    def _request(self, texts: List[str], is_query: bool) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError


class OpenAIEmbedding(_HTTPEmbedding):
    endpoint = "https://api.openai.com/v1/embeddings"
    api_key_env = "OPENAI_API_KEY"

    def __init__(self, model: str = "text-embedding-3-large",
                 dim: Optional[int] = None, api_key: Optional[str] = None):
        # text-embedding-3-large is 3072-dim; MRL lets you request fewer via `dim`.
        super().__init__(model=model, dim=dim or 3072, api_key=api_key)
        self._request_dim = dim  # only sent if explicitly set

    def _request(self, texts: List[str], is_query: bool) -> np.ndarray:
        import httpx
        # OpenAI text-embedding-3-* are symmetric: same call for docs and queries.
        payload = {"model": self.name, "input": texts}
        if self._request_dim:
            payload["dimensions"] = self._request_dim
        r = httpx.post(self.endpoint, json=payload, headers=self._headers(), timeout=60.0)
        r.raise_for_status()
        data = r.json()["data"]
        return np.array([d["embedding"] for d in data], dtype=np.float32)


class VoyageEmbedding(_HTTPEmbedding):
    endpoint = "https://api.voyageai.com/v1/embeddings"
    api_key_env = "VOYAGE_API_KEY"

    def __init__(self, model: str = "voyage-3-large",
                 dim: Optional[int] = None, api_key: Optional[str] = None):
        super().__init__(model=model, dim=dim or 1024, api_key=api_key)

    def _request(self, texts: List[str], is_query: bool) -> np.ndarray:
        import httpx
        # Voyage is asymmetric: documents and queries use different input_type.
        payload = {"model": self.name, "input": texts,
                   "input_type": "query" if is_query else "document"}
        r = httpx.post(self.endpoint, json=payload, headers=self._headers(), timeout=60.0)
        r.raise_for_status()
        data = r.json()["data"]
        return np.array([d["embedding"] for d in data], dtype=np.float32)


def get_provider(provider: str, model: Optional[str] = None,
                 dim: Optional[int] = None) -> EmbeddingProvider:
    provider = (provider or "dev").lower()
    if provider == "dev":
        return DevEmbedding(dim=dim or 256)
    if provider == "openai":
        return OpenAIEmbedding(model=model or "text-embedding-3-large", dim=dim)
    if provider == "voyage":
        return VoyageEmbedding(model=model or "voyage-3-large", dim=dim)
    raise ValueError(
        f"Unknown embedding provider '{provider}'. "
        f"Use one of: dev, openai, voyage (or add your own in embeddings.py)."
    )
