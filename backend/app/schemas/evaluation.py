"""
Pydantic data schemas and corpus validation rules for Gold Evaluation Dataset.
"""
from enum import Enum
from pathlib import Path
from typing import List, Dict, Optional, Any, Set
import json
from pydantic import BaseModel, Field, model_validator


class QueryCategory(str, Enum):
    CLAUSE_FACT = "clause_fact"
    TECHNICAL_SPEC = "technical_spec"
    DEADLINE_EXCEPTION = "deadline_exception"
    ENTITY_APPLICABILITY = "entity_applicability"
    CROSS_CIRCULAR_AMENDMENT = "cross_circular_amendment"
    PARAPHRASE = "paraphrase"
    UNANSWERABLE = "unanswerable"


class Answerability(str, Enum):
    ANSWERABLE = "answerable"
    UNANSWERABLE = "unanswerable"
    NEEDS_REVIEW = "needs_review"


class AnnotationReviewStatus(str, Enum):
    VERIFIED = "verified"
    PENDING_REVIEW = "pending_review"
    NOT_APPLICABLE = "not_applicable"


class EvidenceItem(BaseModel):
    chunk_id: str = Field(..., description="Unique chunk ID in corpus")
    document_id: str = Field(..., description="Parent document ID")
    pdf_page: int = Field(..., ge=1, description="1-indexed physical PDF page number")
    printed_page: Optional[str] = Field(None, description="Printed page in document header/footer")
    provision_ref: Optional[str] = Field(None, description="Clause/Table/Section identifier")
    verbatim_excerpt: str = Field(..., description="Verbatim text excerpt from source chunk")


class DocumentLabels(BaseModel):
    gold_document_ids: List[str] = Field(default_factory=list, description="Target relevant document IDs")
    rationale: Optional[str] = Field(None, description="Reason for document relevance")


class ChunkLabels(BaseModel):
    gold_chunk_ids: List[str] = Field(default_factory=list, description="Target relevant chunk IDs")
    evidence_items: List[EvidenceItem] = Field(default_factory=list, description="Detailed evidence items")


class RegulatoryContext(BaseModel):
    acting_circular_id: Optional[str] = Field(None, description="Acting / modifying circular ID")
    affected_circular_id: Optional[str] = Field(None, description="Affected / base circular ID")
    relationship_type: Optional[str] = Field(None, description="AMENDS, RESCINDS, REFERENCES, or null")
    exceptions_and_scope: Optional[str] = Field(None, description="Specific entity exceptions or caveats")


class AnnotationStatus(BaseModel):
    document_level: AnnotationReviewStatus = Field(..., description="Review status of document labels")
    chunk_level: AnnotationReviewStatus = Field(..., description="Review status of chunk labels")


class GoldQueryRecord(BaseModel):
    query_id: str = Field(..., description="Unique query identifier")
    query_text: str = Field(..., description="Verbatim text of user regulatory question")
    query_category: QueryCategory = Field(..., description="Query classification category")
    answerability: Answerability = Field(..., description="Answerability in current corpus")
    annotation_status: AnnotationStatus = Field(..., description="Progressive annotation review statuses")
    source_text_verified: bool = Field(False, description="Whether excerpts exist as exact substrings")
    provenance_verified: bool = Field(False, description="Whether doc, chunk, and page IDs match corpus")
    annotation_support_verified: bool = Field(False, description="Whether human review confirms evidence supports query")
    document_labels: DocumentLabels = Field(default_factory=DocumentLabels)
    chunk_labels: ChunkLabels = Field(default_factory=ChunkLabels)
    regulatory_context: Optional[RegulatoryContext] = None

    @property
    def is_document_eval_eligible(self) -> bool:
        """
        Record is eligible for document-level scoring if answerable, verified, and has gold docs.
        """
        return (
            self.answerability == Answerability.ANSWERABLE
            and self.annotation_status.document_level == AnnotationReviewStatus.VERIFIED
            and self.annotation_support_verified
            and len(self.document_labels.gold_document_ids) > 0
        )

    @property
    def is_chunk_eval_eligible(self) -> bool:
        """
        Record is eligible for chunk-level scoring if answerable, doc and chunk-level verified, and has gold chunks.
        """
        return (
            self.answerability == Answerability.ANSWERABLE
            and self.annotation_status.document_level == AnnotationReviewStatus.VERIFIED
            and self.annotation_status.chunk_level == AnnotationReviewStatus.VERIFIED
            and self.annotation_support_verified
            and len(self.chunk_labels.gold_chunk_ids) > 0
        )

    @property
    def is_unanswerable_eval_eligible(self) -> bool:
        """
        Record is eligible for unanswerable/abstention evaluation.
        """
        return (
            self.answerability == Answerability.UNANSWERABLE
            and self.annotation_status.document_level == AnnotationReviewStatus.VERIFIED
            and len(self.document_labels.gold_document_ids) == 0
            and len(self.chunk_labels.gold_chunk_ids) == 0
        )

    @model_validator(mode="after")
    def validate_answerability_and_labels(self):
        if self.answerability == Answerability.UNANSWERABLE:
            if len(self.document_labels.gold_document_ids) > 0:
                raise ValueError(f"Unanswerable query '{self.query_id}' cannot have gold_document_ids.")
            if len(self.chunk_labels.gold_chunk_ids) > 0:
                raise ValueError(f"Unanswerable query '{self.query_id}' cannot have gold_chunk_ids.")
            if self.annotation_status.chunk_level == AnnotationReviewStatus.VERIFIED:
                raise ValueError(f"Unanswerable query '{self.query_id}' chunk_level status should be not_applicable.")
        elif self.answerability == Answerability.ANSWERABLE:
            if self.annotation_status.document_level == AnnotationReviewStatus.VERIFIED:
                if not self.document_labels.gold_document_ids:
                    raise ValueError(f"Verified answerable query '{self.query_id}' must have gold_document_ids.")
            if self.annotation_status.chunk_level == AnnotationReviewStatus.VERIFIED:
                if not self.chunk_labels.gold_chunk_ids:
                    raise ValueError(f"Verified chunk-level query '{self.query_id}' must have gold_chunk_ids.")
        return self


class GoldEvaluationDataset(BaseModel):
    dataset_version: str = Field(..., description="Dataset semantic version")
    corpus_scope: str = Field(..., description="Scope description of corpus")
    queries: List[GoldQueryRecord] = Field(default_factory=list, description="List of gold query records")

    @model_validator(mode="after")
    def validate_unique_query_ids(self):
        ids: Set[str] = set()
        for q in self.queries:
            if q.query_id in ids:
                raise ValueError(f"Duplicate query_id found: '{q.query_id}'")
            ids.add(q.query_id)
        return self


def validate_gold_dataset_against_corpus(
    dataset: GoldEvaluationDataset,
    processed_dir: str | Path = "data/processed",
) -> List[str]:
    """
    Validates gold dataset records against actual physical processed corpus chunks.
    Checks:
    - Document IDs exist in manifest / processed_dir
    - Chunk IDs exist in doc/chunks.json
    - Chunk belongs to document
    - PDF page falls within chunk's page_start..page_end
    - Verbatim excerpt is exact substring of chunk.text

    Returns a list of error strings (empty if valid).
    """
    processed_path = Path(processed_dir)
    errors: List[str] = []

    # Load all chunks and docs from processed_dir
    chunks_map: Dict[str, Dict[str, Any]] = {}
    valid_doc_ids: Set[str] = set()

    if not processed_path.exists():
        return [f"Processed directory '{processed_dir}' does not exist."]

    manifest_file = processed_path / "manifest.json"
    if manifest_file.exists():
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            valid_doc_ids = set(manifest.get("documents", {}).keys())
        except Exception as e:
            errors.append(f"Failed to load manifest: {e}")

    for doc_dir in processed_path.iterdir():
        if not doc_dir.is_dir() or doc_dir.name.startswith((".", "_")):
            continue
        valid_doc_ids.add(doc_dir.name)
        chunks_file = doc_dir / "chunks.json"
        if chunks_file.exists():
            try:
                with open(chunks_file, "r", encoding="utf-8") as f:
                    for chunk in json.load(f):
                        chunks_map[chunk["chunk_id"]] = chunk
            except Exception as e:
                errors.append(f"Failed to load chunks for {doc_dir.name}: {e}")

    for q in dataset.queries:
        # Check document IDs
        for doc_id in q.document_labels.gold_document_ids:
            if doc_id not in valid_doc_ids:
                errors.append(f"Query '{q.query_id}': unknown document_id '{doc_id}'")

        # Check chunk labels and evidence
        for chunk_id in q.chunk_labels.gold_chunk_ids:
            if chunk_id not in chunks_map:
                errors.append(f"Query '{q.query_id}': unknown chunk_id '{chunk_id}'")

        for ev in q.chunk_labels.evidence_items:
            if ev.chunk_id not in chunks_map:
                errors.append(f"Query '{q.query_id}': evidence chunk '{ev.chunk_id}' not found in corpus.")
                continue

            real_chunk = chunks_map[ev.chunk_id]
            # Parent doc match
            if real_chunk["document_id"] != ev.document_id:
                errors.append(
                    f"Query '{q.query_id}': evidence chunk '{ev.chunk_id}' parent doc '{real_chunk['document_id']}' != '{ev.document_id}'"
                )

            # Page range match
            if not (real_chunk["page_start"] <= ev.pdf_page <= real_chunk["page_end"]):
                errors.append(
                    f"Query '{q.query_id}': evidence page {ev.pdf_page} out of bounds [{real_chunk['page_start']}..{real_chunk['page_end']}] for '{ev.chunk_id}'"
                )

            # Substring check
            clean_excerpt = ev.verbatim_excerpt.strip()
            clean_chunk_text = real_chunk["text"]
            # Normalize whitespace for robust match if needed, but exact substring preferred
            if clean_excerpt not in clean_chunk_text and " ".join(clean_excerpt.split()) not in " ".join(clean_chunk_text.split()):
                errors.append(
                    f"Query '{q.query_id}': verbatim excerpt not found in chunk '{ev.chunk_id}' text."
                )

    return errors
