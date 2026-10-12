"""
Retrieval package for lexical BM25, dense vector, and hybrid search.
"""
from app.retrieval.tokenizer import tokenize_text
from app.retrieval.bm25 import BM25Index
from app.retrieval.rrf import reciprocal_rank_fusion
from app.retrieval.service import RetrievalService

__all__ = [
    "tokenize_text",
    "BM25Index",
    "DenseIndex",
    "DenseEmbedder",
    "reciprocal_rank_fusion",
    "RetrievalService",
]


def __getattr__(name: str):
    if name == "DenseIndex":
        from app.retrieval.dense import DenseIndex
        return DenseIndex
    if name == "DenseEmbedder":
        from app.retrieval.dense_embedder import DenseEmbedder
        return DenseEmbedder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
