from __future__ import annotations

import math
from typing import Protocol

import httpx

from .text import stable_hash, tokens


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Deterministic feature-hashing embedder over unigrams and bigrams.

    It stands in for a hosted embedding model so retrieval runs offline and the tests are exact.
    Production used a real embedding model; swap in ``OpenAICompatibleEmbedder`` and set
    COURSEDRAFT_EMBED_DIM to its dimension.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        toks = tokens(text)
        feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
        vec = [0.0] * self.dim
        for f in feats:
            h = stable_hash(f)
            vec[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OpenAICompatibleEmbedder:
    """Embeddings from any OpenAI-compatible endpoint, including a local Ollama server."""

    def __init__(self, base_url: str, model: str, dim: int, api_key: str = "unused") -> None:
        self.dim = dim
        self._model = model
        self._client = httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=60
        )

    def embed(self, text: str) -> list[float]:
        r = self._client.post("/embeddings", json={"model": self._model, "input": text})
        r.raise_for_status()
        vec = r.json()["data"][0]["embedding"]
        if len(vec) != self.dim:
            raise ValueError(f"embedding has {len(vec)} dims, schema expects {self.dim}")
        return vec
