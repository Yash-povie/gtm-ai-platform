"""Deterministic offline embeddings (hashed bag-of-words, L2-normalized).

Good enough for sandbox similarity: texts sharing words get higher cosine. Swap for
fastembed/openai via EMBEDDING_PROVIDER in production.
"""

import hashlib
import math
import re

from gtm_core.config import get_settings

_TOKEN = re.compile(r"[a-z0-9&]+")


def embed_text(text: str, dim: int | None = None) -> list[float]:
    dim = dim or get_settings().embedding_dim
    vec = [0.0] * dim
    for tok in _TOKEN.findall(text.lower()):
        h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
        vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))
