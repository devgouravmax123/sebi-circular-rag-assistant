"""
Focused unit tests for Grounded Evidence Schema and Conservative Graph Resolver.

Tests:
1. Verified applicable partial amendment (ISO 27001 in CIR/2024/113 amended by CIR/2025/119).
2. Provision outside known partial-amendment scope is conservatively UNRESOLVED (not falsely assumed ACTIVE_CURRENT).
3. REFERENCES edge does NOT induce supersession (CIR/2025/060 referencing CIR/2025/045).
4. Full-scope supersession edge marks document as SUPERSEDED.
5. Chunk containing multiple provisions where one is partially amended.
6. Ambiguous or missing provision mapping in partially amended doc returns UNRESOLVED.
7. Passage from newest indexed circular is conservatively UNRESOLVED (not falsely assumed ACTIVE_CURRENT).
8. Missing graph relationships / unknown documents handled safely as UNRESOLVED.
9. Preservation of original historical text, scores, page bounds, and rank when UNRESOLVED.
10. Stable, deterministic package aggregation.
"""
import pytest
from app.schemas.graph import GraphEdge, RelationshipType, ScopeType, ConfidenceLevel, ReviewStatus
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.cscrf_graph_data import create_verified_cscrf_graph
from app.schemas.retrieval import RetrievalResult, RetrievalResponse
from app.schemas.grounding import RegulatoryStatus, GroundedEvidencePackage
from app.grounding.resolver import ConservativeGraphResolver, detect_provisions_in_text


@pytest.fixture
def verified_graph():
    return create_verified_cscrf_graph()


@pytest.fixture
def resolver(verified_graph):
    return ConservativeGraphResolver(graph_collection=verified_graph)


def make_result(chunk_id: str, doc_id: str, text: str, rank: int = 1, score: float = 10.0) -> RetrievalResult:
    return RetrievalResult(
        chunk_id=chunk_id,
        document_id=doc_id,
        title=f"Circular {doc_id}",
        circular_number=f"CIR/{doc_id}",
        issue_date="2024-08-20",
        page_start=47,
        page_end=48,
        text=text,
        score=score,
        rank=rank,
        is_ocr=False,
    )


def test_1_verified_applicable_partial_amendment(resolver):
    # CIR/2024/113 chunk discussing ISO 27001 certification
    # CIR/2025/119 contains verified partial amendment on ISO 27001 (Clause 6.11)
    text = "All MIIs and Qualified REs shall obtain ISO 27001 certification within 1 year of issuance of CSCRF."
    res = make_result("sebi_cscrf_2024_113_p47_37", "sebi_cscrf_2024_113", text)
    item = resolver.resolve_item(res)

    assert item.regulatory_status == RegulatoryStatus.PARTIALLY_AMENDED
    assert "sebi_cscrf_2025_119" in item.acting_document_ids
    assert "edge_cscrf_2025_119_amends_2024_113_technical_clarifications" in item.applicable_edge_ids
    assert len(item.mapped_provisions) >= 1
    assert any("ISO 27001" in p.provision_id for p in item.mapped_provisions)


def test_2_provision_outside_amendment_scope_is_conservative_unresolved(resolver):
    # CIR/2024/113 chunk discussing CISO reporting line directly to MD/CEO (GV.GO.S1)
    # This provision is NOT amended in the 5-document pilot corpus, but current validity cannot be established globally
    text = "The reporting of the CISO of the MII and Qualified REs shall be directly to the MD & CEO of their organization."
    res = make_result("sebi_cscrf_2024_113_p80_67", "sebi_cscrf_2024_113", text)
    item = resolver.resolve_item(res)

    assert item.regulatory_status == RegulatoryStatus.UNRESOLVED
    assert len(item.acting_document_ids) == 0
    assert any("GV.GO.S1" in p.provision_id for p in item.mapped_provisions)
    assert "current legal validity is not independently established beyond the corpus" in item.status_reason


def test_3_references_edge_does_not_induce_supersession():
    custom_graph = GraphEdgeCollection()
    custom_graph.add_edge(
        GraphEdge(
            edge_id="ref_edge_test",
            source_document_id="doc_new",
            target_document_id="doc_base",
            relationship=RelationshipType.REFERENCES,
            scope=ScopeType.FULL,
            evidence_text="Reference text",
            review_status=ReviewStatus.VERIFIED,
        )
    )
    custom_resolver = ConservativeGraphResolver(custom_graph)
    custom_res = make_result("doc_base_c1", "doc_base", "General informational text")
    custom_item = custom_resolver.resolve_item(custom_res)

    assert custom_item.regulatory_status == RegulatoryStatus.INFORMATIONAL
    assert custom_item.regulatory_status != RegulatoryStatus.SUPERSEDED
    assert "doc_new" in custom_item.acting_document_ids


def test_4_full_scope_supersession_edge():
    custom_graph = GraphEdgeCollection()
    custom_graph.add_edge(
        GraphEdge(
            edge_id="rescind_edge_test",
            source_document_id="doc_2026",
            target_document_id="doc_old",
            relationship=RelationshipType.RESCINDS,
            scope=ScopeType.FULL,
            evidence_text="Rescission text",
            review_status=ReviewStatus.VERIFIED,
        )
    )
    custom_resolver = ConservativeGraphResolver(custom_graph)
    res = make_result("doc_old_c1", "doc_old", "Old obsolete text")
    item = custom_resolver.resolve_item(res)

    assert item.regulatory_status == RegulatoryStatus.SUPERSEDED
    assert "doc_2026" in item.acting_document_ids
    assert "rescind_edge_test" in item.applicable_edge_ids


def test_5_chunk_containing_multiple_provisions_with_partial_amendment(resolver):
    # Text containing both ISO 27001 (amended by 2025_119) and CISO reporting line (unamended in corpus)
    text = "REs shall obtain ISO 27001 certification. The reporting of the CISO of the MII shall be directly to MD & CEO."
    res = make_result("multi_prov_chunk", "sebi_cscrf_2024_113", text)
    item = resolver.resolve_item(res)

    # Partial amendment on ISO 27001 must be detected
    assert item.regulatory_status == RegulatoryStatus.PARTIALLY_AMENDED
    assert "sebi_cscrf_2025_119" in item.acting_document_ids
    assert len(item.mapped_provisions) >= 2


def test_6_ambiguous_missing_provision_mapping_returns_unresolved(resolver):
    text = "General definitions and miscellaneous terms not mentioning any specific standard or clause."
    res = make_result("sebi_cscrf_2024_113_p1_0", "sebi_cscrf_2024_113", text)
    item = resolver.resolve_item(res)

    assert item.regulatory_status == RegulatoryStatus.UNRESOLVED
    assert "could not be unambiguously mapped" in item.status_reason


def test_7_newest_indexed_circular_is_conservative_unresolved(resolver):
    # A chunk from CIR/2025/119 (newest indexed circular in 5-doc corpus)
    # Must NOT be marked ACTIVE_CURRENT merely because it is the latest circular in the pilot corpus
    text = "Qualified REs are encouraged and recommended (not mandatory) to obtain ISO 27001 certification."
    res = make_result("sebi_cscrf_2025_119_p9_6", "sebi_cscrf_2025_119", text)
    item = resolver.resolve_item(res)

    assert item.regulatory_status == RegulatoryStatus.UNRESOLVED
    assert "current legal validity is not independently established beyond the corpus" in item.status_reason


def test_8_missing_graph_relationships_handled_as_unresolved(resolver):
    res = make_result("unknown_doc_c1", "unknown_doc_id", "Some isolated regulatory text")
    item = resolver.resolve_item(res)

    assert item.regulatory_status == RegulatoryStatus.UNRESOLVED
    assert "could not be unambiguously mapped" in item.status_reason


def test_9_provenance_and_metadata_preservation(resolver):
    res = make_result("chunk_prov_test", "sebi_cscrf_2024_113", "The reporting of the CISO to MD & CEO.", rank=3, score=8.75)
    item = resolver.resolve_item(res)

    assert item.chunk_id == "chunk_prov_test"
    assert item.document_id == "sebi_cscrf_2024_113"
    assert item.page_start == 47
    assert item.page_end == 48
    assert item.retrieval_rank == 3
    assert item.retrieval_score == 8.75
    assert item.text == res.text
    assert item.regulatory_status == RegulatoryStatus.UNRESOLVED


def test_10_resolve_package_deterministic_aggregation(resolver):
    r1 = make_result("c1", "sebi_cscrf_2024_113", "The reporting of the CISO to MD & CEO.", rank=1)
    r2 = make_result("c2", "sebi_cscrf_2024_113", "ISO 27001 certification mandatory for REs.", rank=2)
    response = RetrievalResponse(query="CISO and ISO 27001", total_results=2, results=[r1, r2])

    package = resolver.resolve_package(response)
    assert isinstance(package, GroundedEvidencePackage)
    assert package.total_retrieved == 2
    assert package.unresolved_count == 1
    assert package.partially_amended_count == 1
    assert package.active_count == 0
    assert package.items[0].chunk_id == "c1"
    assert package.items[1].chunk_id == "c2"
