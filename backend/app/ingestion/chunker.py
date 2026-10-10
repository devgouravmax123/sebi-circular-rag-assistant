from typing import List
from app.schemas.document import PageContent, ChunkRecord


class TextChunker:
    """Deterministic chunker with word-level sliding window across page boundaries."""

    def __init__(self, chunk_size_words: int = 350, chunk_overlap_words: int = 40):
        if chunk_overlap_words >= chunk_size_words:
            raise ValueError("chunk_overlap_words must be strictly smaller than chunk_size_words")
        self.chunk_size_words = chunk_size_words
        self.chunk_overlap_words = chunk_overlap_words

    def chunk_document(self, document_id: str, pages: List[PageContent]) -> List[ChunkRecord]:
        """Convert page contents into deterministic chunk records with stable IDs and page boundaries."""
        if not pages:
            return []

        # Build list of words with word-to-page tracking to preserve exact page provenance
        word_tokens: List[str] = []
        word_pages: List[int] = []
        is_ocr_present = any(p.is_ocr for p in pages)

        for page in pages:
            if not page.text.strip():
                continue
            words = page.text.split()
            for w in words:
                word_tokens.append(w)
                word_pages.append(page.page_number)

        if not word_tokens:
            return []

        chunks: List[ChunkRecord] = []
        step = self.chunk_size_words - self.chunk_overlap_words
        total_words = len(word_tokens)
        chunk_idx = 0

        for start_idx in range(0, total_words, step):
            end_idx = min(start_idx + self.chunk_size_words, total_words)
            chunk_words = word_tokens[start_idx:end_idx]
            chunk_text = " ".join(chunk_words)

            page_start = word_pages[start_idx]
            page_end = word_pages[end_idx - 1]

            chunk_id = f"{document_id}_p{page_start}_{chunk_idx}"
            chunks.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    text=chunk_text,
                    page_start=page_start,
                    page_end=page_end,
                    is_ocr=is_ocr_present,
                    chunk_type="body",
                    metadata={
                        "word_count": len(chunk_words),
                        "chunk_index": chunk_idx,
                    },
                )
            )
            chunk_idx += 1

            if end_idx >= total_words:
                break

        return chunks
