"""Deterministic, offline embeddings for RAG tests (no network, no API key)."""
import hashlib
import math
import re

from langchain_core.embeddings import Embeddings

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset("a an and are as at be by for from in is it of on or the to with".split())


class KeywordHashEmbeddings(Embeddings):
    """Bag-of-words feature hashing into `size` dimensions, L2-normalised.

    Unlike langchain_core's DeterministicFakeEmbedding (a hash of the whole text, so unrelated texts get
    unrelated vectors), texts sharing words get similar vectors, which makes retrieval assertions meaningful.
    """

    def __init__(self, size: int = 1536):
        """Set the vector size (must match the kb_chunks.embedding column)."""
        self.size = size

    def _embed(self, text: str) -> list[float]:
        """Hash each non-stopword token into a bucket and normalise."""
        vec = [0.0] * self.size
        for token in _TOKEN.findall(text.lower()):
            if token not in _STOPWORDS:
                vec[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % self.size] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:  # cosine distance to a zero vector is undefined
            vec[0], norm = 1.0, 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed many texts."""
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """Embed one query."""
        return self._embed(text)
