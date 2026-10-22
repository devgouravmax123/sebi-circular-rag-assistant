"""
Citation and evidence validator for the SEBI CSCRF Regulatory Q&A Assistant.

Enforces:
1. Mechanical citation integrity against registered corpus chunks and document metadata.
2. Distinct claim-to-evidence support evaluation without assuming semantic proof from mechanical checks.
3. Graph consistency and conservative regulatory status safety (no upgrading UNRESOLVED, no supersession from REFERENCES).
4. Deterministic, fail-closed handling of invalid provenance or malformed inputs.
"""
import os
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Any, Union

from app.schemas.document import ChunkRecord, DocumentRecord
from app.schemas.grounding import GroundedEvidenceItem, RegulatoryStatus
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ReviewStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.cscrf_graph_data import create_verified_cscrf_graph
from app.schemas.citation import (
    CitationItem,
    CitationStatus,
    CitationValidationResult,
    ClaimItem,
    ClaimSupportStatus,
    ClaimValidationResult,
    ValidationPackageReport,
)


class CitationValidator:
    """
    Validator for mechanical citation integrity, regulatory consistency, and claim support.
    """

    def __init__(
        self,
        processed_dir: Union[str, Path] = "data/processed",
        graph_collection: Optional[GraphEdgeCollection] = None,
        corpus_chunks: Optional[Dict[str, ChunkRecord]] = None,
        corpus_documents: Optional[Dict[str, DocumentRecord]] = None,
        collection_id: Optional[str] = None,
        indexes_dir: Union[str, Path] = "data/indexes",
    ):
        self.processed_dir = Path(processed_dir)
        self.indexes_dir = Path(indexes_dir)
        self.collection_id = collection_id.strip() if collection_id else None
        self.graph = graph_collection if graph_collection is not None else create_verified_cscrf_graph()
        self.corpus_chunks: Dict[str, ChunkRecord] = corpus_chunks if corpus_chunks is not None else {}
        self.corpus_documents: Dict[str, DocumentRecord] = corpus_documents if corpus_documents is not None else {}

        if not self.corpus_chunks or not self.corpus_documents:
            self._load_corpus()

    def _load_corpus(self) -> None:
        """
        Loads chunks and document metadata into in-memory dictionaries.
        If collection_id is specified, attempts to load from active published snapshot for that collection.
        If collection_id is None or 'sebi_cscrf' and snapshot is missing, falls back to processed_dir.
        Fails safely and deterministically without crashing if directory is missing or partially populated.
        """
        # 1. Attempt to load from active published snapshot if collection_id is specified
        if self.collection_id:
            # Check partition pointer: data/indexes/{collection_id}/active_snapshot.json
            partition_dir = self.indexes_dir / self.collection_id
            pointer_file = partition_dir / "active_snapshot.json"
            if not pointer_file.exists() and self.collection_id == "sebi_cscrf":
                pointer_file = self.indexes_dir / "active_snapshot.json"

            if pointer_file.exists():
                try:
                    with open(pointer_file, "r", encoding="utf-8") as f:
                        pointer_data = json.load(f)

                    # Resolve snapshot directory: prefer explicit snapshot_dir, or resolve via snapshot_id
                    if "snapshot_dir" in pointer_data:
                        target_dir = Path(pointer_data["snapshot_dir"])
                        if not target_dir.is_absolute():
                            target_dir = self.indexes_dir / target_dir
                    elif "snapshot_id" in pointer_data:
                        snap_id = pointer_data["snapshot_id"]
                        if self.collection_id == "sebi_cscrf" and not (partition_dir / "snapshots" / snap_id).exists():
                            target_dir = self.indexes_dir / "snapshots" / snap_id
                        else:
                            target_dir = partition_dir / "snapshots" / snap_id
                    else:
                        target_dir = None

                    if target_dir and target_dir.exists():
                        chunks_file = target_dir / "chunks.json"
                        docs_file = target_dir / "documents.json"
                        if chunks_file.exists() and docs_file.exists():
                            with open(chunks_file, "r", encoding="utf-8") as f:
                                chunks_data = json.load(f)
                                if isinstance(chunks_data, list):
                                    for c in chunks_data:
                                        chunk_rec = ChunkRecord(**c)
                                        self.corpus_chunks[chunk_rec.chunk_id] = chunk_rec
                                elif isinstance(chunks_data, dict):
                                    for c in chunks_data.values():
                                        chunk_rec = ChunkRecord(**c)
                                        self.corpus_chunks[chunk_rec.chunk_id] = chunk_rec

                            with open(docs_file, "r", encoding="utf-8") as f:
                                docs_data = json.load(f)
                                if isinstance(docs_data, list):
                                    for d in docs_data:
                                        doc_rec = DocumentRecord(**d)
                                        self.corpus_documents[doc_rec.document_id] = doc_rec
                                elif isinstance(docs_data, dict):
                                    for d in docs_data.values():
                                        doc_rec = DocumentRecord(**d)
                                        self.corpus_documents[doc_rec.document_id] = doc_rec
                            return
                except Exception:
                    pass

        # 2. Legacy fallback to processed_dir (for sebi_cscrf or when no snapshot exists)
        if not self.processed_dir.exists():
            return

        # Load manifest if available
        manifest_path = self.processed_dir / "manifest.json"
        doc_dirs: List[Path] = []
        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                    for doc_id in manifest.get("documents", {}).keys():
                        p = self.processed_dir / doc_id
                        if p.exists() and p.is_dir():
                            doc_dirs.append(p)
            except Exception:
                pass

        if not doc_dirs:
            doc_dirs = [p for p in self.processed_dir.iterdir() if p.is_dir()]

        for d in doc_dirs:
            doc_file = d / "document.json"
            if doc_file.exists():
                try:
                    with open(doc_file, "r", encoding="utf-8") as f:
                        doc_data = json.load(f)
                        doc_rec = DocumentRecord(**doc_data)
                        self.corpus_documents[doc_rec.document_id] = doc_rec
                except Exception:
                    pass

            chunks_file = d / "chunks.json"
            if chunks_file.exists():
                try:
                    with open(chunks_file, "r", encoding="utf-8") as f:
                        chunks_data = json.load(f)
                        for c in chunks_data:
                            chunk_rec = ChunkRecord(**c)
                            self.corpus_chunks[chunk_rec.chunk_id] = chunk_rec
                except Exception:
                    pass

    def validate_citation(self, citation: Union[CitationItem, Dict[str, Any]]) -> CitationValidationResult:
        """
        Mechanically validates a single citation reference against authoritative corpus data.
        """
        if isinstance(citation, dict):
            try:
                citation = CitationItem(**citation)
            except Exception as e:
                return CitationValidationResult(
                    citation=CitationItem(chunk_id="malformed"),
                    status=CitationStatus.INVALID_MALFORMED,
                    is_valid=False,
                    failure_reasons=[f"Malformed citation input: {str(e)}"],
                )

        chunk_id = citation.chunk_id.strip() if citation.chunk_id else ""
        if not chunk_id or chunk_id not in self.corpus_chunks:
            return CitationValidationResult(
                citation=citation,
                status=CitationStatus.INVALID_CHUNK_NOT_FOUND,
                is_valid=False,
                failure_reasons=[f"Chunk ID '{chunk_id}' does not exist in registered corpus."],
            )

        chunk = self.corpus_chunks[chunk_id]
        doc = self.corpus_documents.get(chunk.document_id)
        failure_reasons: List[str] = []

        # 1. Collection ID matching and isolation
        target_collection = self.collection_id or getattr(chunk, "collection_id", None)
        if citation.collection_id:
            cited_coll_id = citation.collection_id.strip()
            # If validator is scoped to a collection, citation must match
            if self.collection_id and cited_coll_id != self.collection_id:
                failure_reasons.append(
                    f"Collection ID mismatch: citation specifies '{cited_coll_id}', "
                    f"but validator is scoped to collection '{self.collection_id}'."
                )
            # If chunk has collection identity, citation must match chunk's collection
            elif chunk.collection_id and cited_coll_id != chunk.collection_id:
                failure_reasons.append(
                    f"Collection ID mismatch: citation specifies '{cited_coll_id}', "
                    f"but registered chunk belongs to collection '{chunk.collection_id}'."
                )

        # Ensure chunk itself matches validator collection scope if validator is scoped
        if self.collection_id and chunk.collection_id and chunk.collection_id != self.collection_id:
            failure_reasons.append(
                f"Cross-collection reference: referenced chunk '{chunk_id}' belongs to collection "
                f"'{chunk.collection_id}', but validator is scoped to '{self.collection_id}'."
            )

        # 2. Parent document ID matching
        if citation.document_id:
            cited_doc_id = citation.document_id.strip()
            if cited_doc_id != chunk.document_id:
                failure_reasons.append(
                    f"Document ID mismatch: citation specifies '{cited_doc_id}', "
                    f"but registered chunk belongs to parent '{chunk.document_id}'."
                )

        # 3. Metadata consistency (title, circular_number, issue_date)
        if doc:
            if citation.title and doc.title:
                c_title_norm = re.sub(r"[\s\-_/]+", " ", citation.title.strip().lower())
                d_title_norm = re.sub(r"[\s\-_/]+", " ", doc.title.strip().lower())
                if c_title_norm != d_title_norm and citation.title.strip() != doc.title.strip():
                    failure_reasons.append(
                        f"Document title mismatch: citation specifies '{citation.title}', "
                        f"registered document has '{doc.title}'."
                    )
            if citation.circular_number and doc.circular_number:
                # Compare normalized circular numbers
                c_norm = re.sub(r"[\s\-_/]+", "", citation.circular_number.lower())
                d_norm = re.sub(r"[\s\-_/]+", "", doc.circular_number.lower())
                if c_norm != d_norm and citation.circular_number.strip() != doc.circular_number.strip():
                    failure_reasons.append(
                        f"Circular number mismatch: citation specifies '{citation.circular_number}', "
                        f"registered document has '{doc.circular_number}'."
                    )
            if citation.issue_date and doc.issue_date:
                if citation.issue_date.strip() != doc.issue_date.strip():
                    failure_reasons.append(
                        f"Issue date mismatch: citation specifies '{citation.issue_date}', "
                        f"registered document has '{doc.issue_date}'."
                    )

        # 4. Page range containment
        chunk_p_start = chunk.page_start
        chunk_p_end = chunk.page_end

        if citation.page is not None:
            if citation.page < chunk_p_start or citation.page > chunk_p_end:
                failure_reasons.append(
                    f"Cited page {citation.page} falls outside chunk page range [{chunk_p_start}, {chunk_p_end}]."
                )

        if citation.page_start is not None:
            if citation.page_start < chunk_p_start or citation.page_start > chunk_p_end:
                failure_reasons.append(
                    f"Cited start page {citation.page_start} is outside chunk page range [{chunk_p_start}, {chunk_p_end}]."
                )

        if citation.page_end is not None:
            if citation.page_end > chunk_p_end or citation.page_end < chunk_p_start:
                failure_reasons.append(
                    f"Cited end page {citation.page_end} is outside chunk page range [{chunk_p_start}, {chunk_p_end}]."
                )

        if citation.page_start is not None and citation.page_end is not None:
            if citation.page_start > citation.page_end:
                failure_reasons.append(
                    f"Inverted page range: page_start ({citation.page_start}) > page_end ({citation.page_end})."
                )

        # 5. Quotation exact substring validation
        if citation.quote:
            quote_clean = citation.quote.strip()
            # Normalize internal whitespace for robust comparison if exact raw match fails
            if quote_clean and quote_clean not in chunk.text:
                # Try normalized whitespace comparison
                norm_quote = re.sub(r"\s+", " ", quote_clean)
                norm_chunk = re.sub(r"\s+", " ", chunk.text)
                if norm_quote not in norm_chunk:
                    failure_reasons.append(
                        f"Quotation '{quote_clean[:60]}...' is not a verbatim substring of referenced chunk '{chunk_id}'."
                    )

        # Classify status
        if failure_reasons:
            if any("Collection ID mismatch" in r or "Cross-collection" in r for r in failure_reasons):
                status = CitationStatus.INVALID_COLLECTION_MISMATCH
            elif any("Document ID mismatch" in r for r in failure_reasons):
                status = CitationStatus.INVALID_DOC_ID_MISMATCH
            elif any("Quotation" in r for r in failure_reasons):
                status = CitationStatus.INVALID_QUOTATION
            elif any("page" in r.lower() for r in failure_reasons):
                status = CitationStatus.INVALID_PAGE_RANGE
            else:
                status = CitationStatus.INVALID_METADATA_MISMATCH
            is_valid = False
        else:
            status = CitationStatus.VALID
            is_valid = True

        resolved_coll = chunk.collection_id or (doc.collection_id if doc else None) or self.collection_id
        return CitationValidationResult(
            citation=citation,
            status=status,
            is_valid=is_valid,
            failure_reasons=failure_reasons,
            authoritative_collection_id=resolved_coll,
            authoritative_document_id=chunk.document_id,
            authoritative_title=doc.title if doc else None,
            authoritative_source_url=doc.source_url if doc else None,
            authoritative_circular_number=doc.circular_number if doc else None,
            authoritative_issue_date=doc.issue_date if doc else None,
            authoritative_page_start=chunk.page_start,
            authoritative_page_end=chunk.page_end,
        )

    def validate_claim(
        self,
        claim: Union[ClaimItem, Dict[str, Any]],
        grounded_items: Optional[List[GroundedEvidenceItem]] = None,
    ) -> ClaimValidationResult:
        """
        Validates a discrete claim against its cited evidence items and graph state.
        Ensures:
        - Mechanical integrity does NOT equate to semantic proof.
        - Unresolved historical status is flagged when current status is claimed.
        - REFERENCES edges are not accepted as evidence of supersession.
        - Unsupported/missing citations fail deterministically.
        """
        if isinstance(claim, dict):
            try:
                claim = ClaimItem(**claim)
            except Exception as e:
                return ClaimValidationResult(
                    claim=ClaimItem(claim_id="malformed", claim_text=""),
                    support_status=ClaimSupportStatus.UNSUPPORTED,
                    is_supported=False,
                    citations_mechanically_valid=False,
                    status_reason=f"Malformed claim input: {str(e)}",
                )

        if not claim.cited_chunk_ids:
            return ClaimValidationResult(
                claim=claim,
                support_status=ClaimSupportStatus.UNSUPPORTED,
                is_supported=False,
                citations_mechanically_valid=False,
                status_reason="Claim has no cited evidence items (empty citation list).",
            )

        # 1. Validate mechanical citations
        valid_chunks: List[ChunkRecord] = []
        for cid in claim.cited_chunk_ids:
            c_res = self.validate_citation(CitationItem(chunk_id=cid))
            if not c_res.is_valid:
                return ClaimValidationResult(
                    claim=claim,
                    support_status=ClaimSupportStatus.UNSUPPORTED,
                    is_supported=False,
                    citations_mechanically_valid=False,
                    status_reason=f"Claim cites invalid or non-existent chunk '{cid}': {'; '.join(c_res.failure_reasons)}",
                )
            valid_chunks.append(self.corpus_chunks[cid])

        # 2. Match with GroundedEvidenceItems if provided
        grounded_map: Dict[str, GroundedEvidenceItem] = {}
        if grounded_items:
            grounded_map = {item.chunk_id: item for item in grounded_items}

        flags: List[str] = []

        # 3. Regulatory Graph & Status Invariant Checks
        for c in valid_chunks:
            g_item = grounded_map.get(c.chunk_id)
            reg_status = g_item.regulatory_status if g_item else RegulatoryStatus.UNRESOLVED

            # Check for current status assertion on UNRESOLVED or SUPERSEDED evidence
            if claim.asserts_current_status:
                if reg_status == RegulatoryStatus.UNRESOLVED:
                    flags.append(
                        f"Chunk '{c.chunk_id}' has UNRESOLVED regulatory status. Asserting active/current legal "
                        f"validity without verified graph evidence is uncalibrated."
                    )
                elif reg_status == RegulatoryStatus.SUPERSEDED:
                    return ClaimValidationResult(
                        claim=claim,
                        support_status=ClaimSupportStatus.CONTRADICTORY,
                        is_supported=False,
                        citations_mechanically_valid=True,
                        status_reason=(
                            f"Claim asserts current regulatory status, but cited chunk '{c.chunk_id}' "
                            f"belongs to superseded document/provision '{c.document_id}'."
                        ),
                        applicable_chunk_ids=[c.chunk_id for c in valid_chunks],
                        flags=flags,
                    )

            # Check for supersession claim based on REFERENCES edge
            if claim.asserts_supersession:
                # Check incoming edges to this document in graph
                incoming = self.graph.get_incoming_edges(c.document_id)
                # If only REFERENCES edges exist, flag misrepresentation
                ref_edges = [e for e in incoming if e.relationship == RelationshipType.REFERENCES and e.review_status == ReviewStatus.VERIFIED]
                amend_edges = [e for e in incoming if e.relationship in (RelationshipType.AMENDS, RelationshipType.RESCINDS) and e.review_status == ReviewStatus.VERIFIED]
                if ref_edges and not amend_edges:
                    flags.append(
                        f"Document '{c.document_id}' is only referenced (non-superseding REFERENCES edge) by newer circulars; "
                        f"presenting this relationship as supersession or amendment is invalid."
                    )
                    return ClaimValidationResult(
                        claim=claim,
                        support_status=ClaimSupportStatus.CONTRADICTORY,
                        is_supported=False,
                        citations_mechanically_valid=True,
                        status_reason=f"Supersession asserted on non-superseding REFERENCES edge for '{c.document_id}'.",
                        applicable_chunk_ids=[c.chunk_id for c in valid_chunks],
                        flags=flags,
                    )

        if flags and any("UNRESOLVED" in f for f in flags):
            return ClaimValidationResult(
                claim=claim,
                support_status=ClaimSupportStatus.FLAGGED_UNRESOLVED,
                is_supported=False,
                citations_mechanically_valid=True,
                status_reason="Claim relies on evidence with UNRESOLVED regulatory status while asserting current legal validity.",
                applicable_chunk_ids=[c.chunk_id for c in valid_chunks],
                flags=flags,
            )

        # 4. Deterministic vs Semantic Support Distinction
        # Exact quote or deterministic provision support:
        combined_text = " ".join(c.text for c in valid_chunks)

        # Check if the claim text itself is directly quoted verbatim in chunk text
        claim_norm = re.sub(r"\s+", " ", claim.claim_text.strip().lower())
        text_norm = re.sub(r"\s+", " ", combined_text.lower())

        if len(claim_norm) > 10 and claim_norm in text_norm:
            support_status = ClaimSupportStatus.SUPPORTED_DETERMINISTIC
            is_supported = True
            reason = "Claim text is verified deterministically as an exact or verbatim quotation from cited chunk(s)."
        else:
            # Mechanical citation holds, but semantic entailment cannot be guaranteed without LLM/human review
            support_status = ClaimSupportStatus.NEEDS_SEMANTIC_REVIEW
            is_supported = False
            reason = "Mechanical citations are valid, but semantic claim entailment requires review (not deterministically provable by substring)."

        return ClaimValidationResult(
            claim=claim,
            support_status=support_status,
            is_supported=is_supported,
            citations_mechanically_valid=True,
            status_reason=reason,
            applicable_chunk_ids=[c.chunk_id for c in valid_chunks],
            flags=flags,
        )

    def validate_package(
        self,
        citations: List[Union[CitationItem, Dict[str, Any]]],
        claims: Optional[List[Union[ClaimItem, Dict[str, Any]]]] = None,
        grounded_items: Optional[List[GroundedEvidenceItem]] = None,
    ) -> ValidationPackageReport:
        """
        Validates an entire evidence package including citations and claims.
        """
        citation_results = [self.validate_citation(c) for c in citations]
        all_citations_valid = all(r.is_valid for r in citation_results) if citation_results else True
        valid_count = sum(1 for r in citation_results if r.is_valid)

        claim_results: List[ClaimValidationResult] = []
        if claims:
            claim_results = [self.validate_claim(cl, grounded_items=grounded_items) for cl in claims]

        summary_flags: List[str] = []
        if not all_citations_valid:
            summary_flags.append(f"{len(citation_results) - valid_count} citation(s) failed mechanical validation.")

        has_contradictions = any(cr.support_status == ClaimSupportStatus.CONTRADICTORY for cr in claim_results)
        if has_contradictions:
            summary_flags.append("One or more claims contradict verified regulatory graph or status invariants.")

        for cr in claim_results:
            summary_flags.extend(cr.flags)

        # Deduplicate flags while preserving order
        unique_flags = list(dict.fromkeys(summary_flags))

        # Synthesis is blocked if mechanical citations fail or if claims are actively contradictory
        may_proceed = all_citations_valid and not has_contradictions

        return ValidationPackageReport(
            all_citations_valid=all_citations_valid,
            may_proceed_to_synthesis=may_proceed,
            total_citations=len(citation_results),
            valid_citations_count=valid_count,
            citation_results=citation_results,
            claim_results=claim_results,
            summary_flags=unique_flags,
        )
