"""
Deterministic, zero-dependency BM25 (Okapi) lexical search engine.
"""
import math
from typing import List, Dict, Tuple, Optional, Any, Set
from collections import Counter

from app.schemas.document import ChunkRecord, DocumentRecord
from app.schemas.retrieval import RetrievalResult, RetrievalResponse, RetrievalFilter
from app.retrieval.tokenizer import tokenize_text


def _matches_filter(
    chunk: ChunkRecord,
    doc_meta: Optional[DocumentRecord],
    filters: Optional[RetrievalFilter],
) -> bool:
    """
    Evaluates whether a chunk and its parent document satisfy the optional retrieval filter.
    Returns True if no filter or all active filter clauses match; False otherwise.
    """
    if filters is None:
        return True

    # 1. Document ID filter
    if filters.document_id and chunk.document_id != filters.document_id:
        return False

    if doc_meta is not None:
        # 2. Regulator filter
        if filters.regulator:
            doc_reg = (getattr(doc_meta, "regulator", None) or "SEBI").upper()
            if doc_reg != filters.regulator.upper():
                return False

        # 3. Domain filter
        if filters.domain:
            doc_dom = (getattr(doc_meta, "domain", None) or "CSCRF").upper()
            if doc_dom != filters.domain.upper():
                return False

        # 4. Date range filters (issue_date: YYYY-MM-DD)
        if filters.start_date:
            if not doc_meta.issue_date or doc_meta.issue_date < filters.start_date:
                return False
        if filters.end_date:
            if not doc_meta.issue_date or doc_meta.issue_date > filters.end_date:
                return False
    else:
        # If no document metadata available, reject if domain/regulator/date filters are active
        if filters.domain or filters.start_date or filters.end_date:
            return False

    return True


class BM25Index:
    """
    In-memory, deterministic BM25 (Okapi) lexical retrieval index.

    Parameters:
    - k1: Term frequency saturation parameter (default: 1.5)
    - b: Document length normalization parameter (default: 0.75)
    - epsilon: Floor parameter for negative/zero IDF terms (default: 0.25)
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75, epsilon: float = 0.25):
        self.k1 = k1
        self.b = b
        self.epsilon = epsilon

        # Indexed documents / chunks
        self.chunks: List[ChunkRecord] = []
        self.doc_metadata: Dict[str, DocumentRecord] = {}

        # Corpus statistics
        self.corpus_size: int = 0
        self.avg_doc_len: float = 0.0
        self.doc_lengths: List[int] = []

        # Term frequencies per chunk and inverted document frequencies
        self.term_freqs: List[Counter] = []
        self.inverted_index: Dict[str, List[int]] = {}  # term -> list of chunk indices
        self.idf: Dict[str, float] = {}

    def build_from_chunks(
        self,
        chunks: List[ChunkRecord],
        documents: Optional[Dict[str, DocumentRecord]] = None,
    ) -> "BM25Index":
        """
        Build the BM25 index from a list of ChunkRecord objects and optional DocumentRecord metadata.
        """
        self.chunks = list(chunks)
        self.doc_metadata = documents if documents is not None else {}
        self.corpus_size = len(self.chunks)

        if self.corpus_size == 0:
            self.avg_doc_len = 0.0
            self.doc_lengths = []
            self.term_freqs = []
            self.inverted_index = {}
            self.idf = {}
            return self

        total_len = 0
        self.doc_lengths = []
        self.term_freqs = []
        self.inverted_index = {}

        # Tokenize each chunk and calculate term frequencies
        for idx, chunk in enumerate(self.chunks):
            tokens = tokenize_text(chunk.text)
            doc_len = len(tokens)
            self.doc_lengths.append(doc_len)
            total_len += doc_len

            tf = Counter(tokens)
            self.term_freqs.append(tf)

            for term in tf.keys():
                if term not in self.inverted_index:
                    self.inverted_index[term] = []
                self.inverted_index[term].append(idx)

        self.avg_doc_len = total_len / self.corpus_size if self.corpus_size > 0 else 0.0

        # Calculate standard Okapi BM25 IDF:
        # IDF(q) = ln( (N - n(q) + 0.5) / (n(q) + 0.5) + 1.0 )
        self.idf = {}
        for term, doc_indices in self.inverted_index.items():
            n_q = len(doc_indices)
            idf_val = math.log((self.corpus_size - n_q + 0.5) / (n_q + 0.5) + 1.0)
            # Ensure non-negative IDF via epsilon smoothing
            self.idf[term] = max(idf_val, self.epsilon)

        return self

    def search(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[RetrievalFilter] = None,
    ) -> RetrievalResponse:
        """
        Execute BM25 lexical search over indexed chunks with optional pre-fusion metadata filtering.

        Features:
        - Deterministic scoring.
        - Pre-fusion filtering: discards candidates outside filter criteria before ranking.
        - Stable tie-breaker: sort primarily by score descending, secondarily by chunk_id ascending.
        - Safe validation: empty queries return empty results; invalid top_k is handled safely.
        """
        if top_k <= 0 or self.corpus_size == 0:
            return RetrievalResponse(query=query, total_results=0, results=[])

        query_tokens = tokenize_text(query)
        if not query_tokens:
            return RetrievalResponse(query=query, total_results=0, results=[])

        # Candidate chunks that contain at least one query term
        candidate_indices: Set[int] = set()
        for token in query_tokens:
            if token in self.inverted_index:
                candidate_indices.update(self.inverted_index[token])

        if not candidate_indices:
            return RetrievalResponse(query=query, total_results=0, results=[])

        # Score candidate chunks
        scores: List[Tuple[float, str, int]] = []  # (score, chunk_id, idx)

        for idx in candidate_indices:
            chunk = self.chunks[idx]
            doc_meta = self.doc_metadata.get(chunk.document_id)

            # Apply pre-ranking metadata filter
            if not _matches_filter(chunk, doc_meta, filters):
                continue

            score = 0.0
            doc_len = self.doc_lengths[idx]
            tf_map = self.term_freqs[idx]
            len_norm = 1.0 - self.b + self.b * (doc_len / self.avg_doc_len if self.avg_doc_len > 0 else 1.0)

            for token in query_tokens:
                if token in tf_map:
                    tf = tf_map[token]
                    idf = self.idf[token]
                    # Okapi BM25 formula: IDF * (TF * (k1 + 1)) / (TF + k1 * (1 - b + b * (doc_len / avg_len)))
                    num = tf * (self.k1 + 1.0)
                    denom = tf + self.k1 * len_norm
                    score += idf * (num / denom)

            if score > 0.0:
                scores.append((score, chunk.chunk_id, idx))

        # Stable sort: score DESC, chunk_id ASC
        scores.sort(key=lambda x: (-x[0], x[1]))

        # Limit to top_k
        top_scores = scores[:top_k]

        results: List[RetrievalResult] = []
        for rank, (score, _, idx) in enumerate(top_scores, start=1):
            chunk = self.chunks[idx]
            doc_meta = self.doc_metadata.get(chunk.document_id)

            result_item = RetrievalResult(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                title=doc_meta.title if doc_meta else None,
                circular_number=doc_meta.circular_number if doc_meta else None,
                issue_date=doc_meta.issue_date if doc_meta else None,
                effective_date=doc_meta.effective_date if doc_meta else None,
                regulator=getattr(doc_meta, "regulator", "SEBI") if doc_meta else "SEBI",
                domain=getattr(doc_meta, "domain", "CSCRF") if doc_meta else "CSCRF",
                sha256=doc_meta.sha256 if doc_meta else None,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                text=chunk.text,
                score=round(score, 6),
                rank=rank,
                is_ocr=chunk.is_ocr,
                metadata=chunk.metadata,
            )
            results.append(result_item)

        return RetrievalResponse(
            query=query,
            total_results=len(results),
            results=results,
        )
