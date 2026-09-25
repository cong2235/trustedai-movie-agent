"""Embedding backends behind one interface, so the index, search and eval can swap them.

    bge-small       BAAI/bge-small-en-v1.5, local CPU, 384-d, free, ~29 min to embed the catalogue
    openai-3-small  OpenAI text-embedding-3-small, 1536-d, ~4M tokens for the catalogue (~$0.08), ~2 min

Artifacts are stored per backend (artifacts/<name>/) so both can coexist and be compared.
"""

from __future__ import annotations

import time
from functools import cached_property

import numpy as np

from . import config

BACKENDS = ("bge-small", "openai-3-small")


class LocalBGE:
    name = "bge-small"
    model_id = "BAAI/bge-small-en-v1.5"
    query_prefix = "Represent this sentence for searching relevant passages: "

    @cached_property
    def model(self):
        from sentence_transformers import SentenceTransformer  # heavy import, keep lazy
        return SentenceTransformer(self.model_id)

    def encode_docs(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        return self.model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                 show_progress_bar=True, convert_to_numpy=True).astype(np.float32)

    def encode_query(self, query: str) -> np.ndarray:
        return self.model.encode([self.query_prefix + query], normalize_embeddings=True)[0].astype(np.float32)


class OpenAIEmbedder:
    name = "openai-3-small"
    model_id = "text-embedding-3-small"

    @cached_property
    def client(self):
        import openai
        return openai.OpenAI(timeout=60.0, max_retries=0)   # retries are handled in _embed

    def _embed(self, batch: list[str], timeout: float = 60.0, attempts: int = 5) -> np.ndarray:
        for attempt in range(attempts):
            try:
                resp = self.client.with_options(timeout=timeout).embeddings.create(model=self.model_id, input=batch)
                return np.array([d.embedding for d in resp.data], dtype=np.float32)
            except Exception as e:  # rate limits / transient network errors: back off and retry
                if attempt == attempts - 1:
                    raise
                wait = 2 ** attempt
                print(f"  embedding batch failed ({type(e).__name__}); retrying in {wait}s")
                time.sleep(wait)

    def encode_docs(self, texts: list[str], batch_size: int = 256) -> np.ndarray:
        out = []
        for i in range(0, len(texts), batch_size):
            out.append(self._embed([t or " " for t in texts[i:i + batch_size]]))
            print(f"  embedded {min(i + batch_size, len(texts))}/{len(texts)} chunks", flush=True)
        vecs = np.vstack(out)
        return vecs / (np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9)

    def encode_query(self, query: str) -> np.ndarray:
        v = self._embed([query], timeout=10.0, attempts=2)[0]   # interactive path: fail fast
        return v / (np.linalg.norm(v) + 1e-9)


def get_embedder(name: str | None = None):
    name = name or config.EMBED_BACKEND
    if name == "bge-small":
        return LocalBGE()
    if name == "openai-3-small":
        return OpenAIEmbedder()
    raise ValueError(f"Unknown embedding backend {name!r}; choose from {BACKENDS}")


def artifact_dir(name: str | None = None):
    return config.ARTIFACT_DIR / (name or config.EMBED_BACKEND)
