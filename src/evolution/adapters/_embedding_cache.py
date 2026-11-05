"""Content-hash on-disk cache for embeddings.

Wraps a langchain ``Embeddings``-shaped object so identical inputs hit a
shared on-disk cache instead of round-tripping to OpenRouter. Keyed by
``sha256(model_id + "\\n" + text)``. Each entry is one tiny JSON file.

Why this exists: MemP rebuilds its FAISS index on every produce run, and
``Memory`` calls ``embed_query`` for every retrieved-procedure search.
Across N-trial reruns this multiplies the OpenRouter spend with no
benefit — every call returns the same vector for the same input. The
cache makes reruns free for unchanged inputs while preserving correctness
when inputs change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import Lock
from typing import Any


class CachedEmbeddings:
    """Memoise an inner ``Embeddings`` instance by sha256(model+text)."""

    def __init__(self, inner, *, cache_dir: Path, model_id: str) -> None:
        self._inner = inner
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._model = model_id
        self._lock = Lock()

    def _key(self, text: str) -> str:
        h = hashlib.sha256()
        h.update(self._model.encode("utf-8"))
        h.update(b"\n")
        h.update(text.encode("utf-8"))
        return h.hexdigest()

    def _coerce_vector(self, value: Any, *, key: str) -> list[float] | None:
        if not isinstance(value, list) or not value:
            return None
        if not all(isinstance(x, (int, float)) for x in value):
            return None
        return [float(x) for x in value]

    def _load(self, key: str) -> list[float] | None:
        p = self._dir / f"{key}.json"
        if not p.is_file():
            return None
        try:
            value = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt entry — drop it so we re-embed.
            try:
                p.unlink()
            except OSError:
                pass
            return None
        vec = self._coerce_vector(value, key=key)
        if vec is None:
            try:
                p.unlink()
            except OSError:
                pass
            return None
        return vec

    def _save(self, key: str, vec: list[float]) -> None:
        coerced = self._coerce_vector(vec, key=key)
        if coerced is None:
            return
        (self._dir / f"{key}.json").write_text(json.dumps(coerced), encoding="utf-8")

    def embed_query(self, text: str) -> list[float]:
        k = self._key(text)
        v = self._load(k)
        if v is not None:
            return v
        with self._lock:
            v = self._load(k)
            if v is not None:
                return v
            v = self._inner.embed_query(text)
            self._save(k, v)
            return v

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]

    # Forward attribute access so callers that introspect the wrapped
    # client (e.g. for model id) keep working.
    def __getattr__(self, name: str):
        return getattr(self._inner, name)
