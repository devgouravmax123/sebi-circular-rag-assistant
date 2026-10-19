"""
Local dense embedding adapter using sentence-transformers.
"""
from typing import List, Optional
import numpy as np

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIM = 384


class DenseEmbedder:
    """
    Adapter for generating dense vector embeddings locally using SentenceTransformers.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL_NAME,
        normalize_embeddings: bool = True,
        device: Optional[str] = "cpu",
    ):
        self.model_name = model_name
        self.normalize_embeddings = normalize_embeddings
        self.device = device
        self._model = None

    def _load_model(self):
        """
        Lazily load the SentenceTransformer model.
        """
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name, device=self.device)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to load embedding model '{self.model_name}': {e}"
                ) from e

    def embed_texts(self, texts: List[str], batch_size: int = 32) -> np.ndarray:
        """
        Encode a list of texts into a 2D float32 NumPy array of shape (N, dim).
        """
        if not texts:
            return np.empty((0, DEFAULT_EMBEDDING_DIM), dtype=np.float32)

        self._load_model()
        assert self._model is not None
        embeddings = self._model.encode(
            texts,
            batch_size=batch_size,
            show_progress_bar=False,
            normalize_embeddings=self.normalize_embeddings,
            convert_to_numpy=True,
        )

        arr = np.asarray(embeddings, dtype=np.float32)
        if not np.all(np.isfinite(arr)):
            raise ValueError("Generated embeddings contain non-finite values (NaN or Inf).")

        return arr

    def embed_query(self, query: str) -> np.ndarray:
        """
        Encode a single search query into a 1D float32 NumPy array of shape (dim,).
        Empty or whitespace-only queries return an empty 1D array without invoking the model.
        """
        if not query or not query.strip():
            return np.empty((0,), dtype=np.float32)

        self._load_model()
        assert self._model is not None
        # BGE models can take query as plain text or with prompt if needed, standard retrieval uses plain or 'Represent this sentence for searching relevant passages: '
        # BGE v1.5 standard evaluation uses direct text without compulsory prefix or standard instruction
        embedding = self._model.encode(
            query,
            normalize_embeddings=self.normalize_embeddings,
            convert_to_numpy=True,
        )

        arr = np.asarray(embedding, dtype=np.float32).flatten()
        if not np.all(np.isfinite(arr)):
            raise ValueError("Generated query embedding contains non-finite values (NaN or Inf).")

        return arr
