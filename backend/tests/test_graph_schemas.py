import pytest
from pydantic import ValidationError
from app.schemas.graph import (
    GraphEdge,
    RelationshipType,
    ScopeType,
    ConfidenceLevel,
    ReviewStatus,
)
from app.schemas.graph_collection import GraphEdgeCollection


def test_valid_amends_edge():
    """Test creating a valid AMENDS edge with direction Newer -> AMENDS -> Older."""
    edge = GraphEdge(
        edge_id="edge_2024_amends_2020",
        source_document_id="sebi_cir_2024_01",  # Newer/acting
        target_document_id="sebi_cir_2020_05",  # Older/affected
        relationship=RelationshipType.AMENDS,
        scope=ScopeType.PARTIAL,
        affected_provisions=["Clause 3.1", "Paragraph 4(a)"],
        evidence_text="Clause 3.1 of Circular SEBI/CIR/2020/05 stands modified as follows.",
        evidence_page=2,
        confidence=ConfidenceLevel.HIGH,
        review_status=ReviewStatus.VERIFIED,
        effective_date="2024-04-01",
    )
    assert edge.source_document_id == "sebi_cir_2024_01"
    assert edge.target_document_id == "sebi_cir_2020_05"
    assert edge.relationship == RelationshipType.AMENDS
    assert edge.is_supersession_inducing is True
    assert "Clause 3.1" in edge.affected_provisions


def test_valid_rescinds_edge_full_scope():
    """Test creating a valid RESCINDS edge with full scope."""
    edge = GraphEdge(
        edge_id="edge_2024_rescinds_2018",
        source_document_id="sebi_cir_2024_02",
        target_document_id="sebi_cir_2018_10",
        relationship=RelationshipType.RESCINDS,
        scope=ScopeType.FULL,
        evidence_text="The Master Circular SEBI/HO/2018/10 is hereby rescinded.",
        evidence_page=1,
        confidence=ConfidenceLevel.HIGH,
        review_status=ReviewStatus.VERIFIED,
    )
    assert edge.relationship == RelationshipType.RESCINDS
    assert edge.scope == ScopeType.FULL
    assert edge.is_supersession_inducing is True


def test_references_does_not_induce_supersession():
    """Test that a REFERENCES relationship is informational and not supersession-inducing."""
    edge = GraphEdge(
        edge_id="edge_2024_ref_2021",
        source_document_id="sebi_cir_2024_03",
        target_document_id="sebi_cir_2021_08",
        relationship=RelationshipType.REFERENCES,
        scope=ScopeType.FULL,
        evidence_text="Attention is drawn to Circular SEBI/CIR/2021/08 regarding disclosure formats.",
        evidence_page=1,
        confidence=ConfidenceLevel.MEDIUM,
        review_status=ReviewStatus.NEEDS_REVIEW,
    )
    assert edge.relationship == RelationshipType.REFERENCES
    assert edge.is_supersession_inducing is False


def test_invalid_relationship_type():
    """Test that unrecognized relationship types are rejected by schema."""
    with pytest.raises(ValidationError):
        GraphEdge(
            edge_id="edge_invalid_rel",
            source_document_id="doc_new",
            target_document_id="doc_old",
            relationship="MODIFIES",  # Invalid type (must be AMENDS, RESCINDS, or REFERENCES)
            evidence_text="Some text",
        )


def test_self_reference_rejected():
    """Test that self-referential relationships are rejected by validator."""
    with pytest.raises(ValidationError, match="Self-referential relationships are invalid"):
        GraphEdge(
            edge_id="edge_self",
            source_document_id="sebi_cir_2024_01",
            target_document_id="sebi_cir_2024_01",
            relationship=RelationshipType.AMENDS,
            evidence_text="Self amending sentence",
        )


def test_empty_or_whitespace_document_ids_rejected():
    """Test that empty or blank document IDs are rejected."""
    with pytest.raises(ValidationError):
        GraphEdge(
            edge_id="edge_empty_source",
            source_document_id="   ",
            target_document_id="doc_target",
            relationship=RelationshipType.REFERENCES,
            evidence_text="Evidence",
        )

    with pytest.raises(ValidationError):
        GraphEdge(
            edge_id="edge_empty_target",
            source_document_id="doc_source",
            target_document_id="",
            relationship=RelationshipType.REFERENCES,
            evidence_text="Evidence",
        )


def test_partial_scope_requires_affected_provisions():
    """Test that partial scope requires explicit provision list."""
    with pytest.raises(ValidationError, match="When scope is 'partial', 'affected_provisions' must contain at least one"):
        GraphEdge(
            edge_id="edge_partial_no_provisions",
            source_document_id="doc_new",
            target_document_id="doc_old",
            relationship=RelationshipType.AMENDS,
            scope=ScopeType.PARTIAL,
            affected_provisions=[],  # Invalid: cannot be empty for partial scope
            evidence_text="Partially amended",
        )


def test_graph_edge_collection_duplicate_edges():
    """Test duplicate detection in GraphEdgeCollection."""
    collection = GraphEdgeCollection()

    edge1 = GraphEdge(
        edge_id="edge_001",
        source_document_id="doc_b",
        target_document_id="doc_a",
        relationship=RelationshipType.AMENDS,
        evidence_text="Amends doc_a",
    )
    collection.add_edge(edge1)
    assert len(collection) == 1

    # Attempting to add with same edge_id
    with pytest.raises(ValueError, match="Duplicate edge_id"):
        collection.add_edge(edge1)

    # Attempting to add different edge_id but duplicate (source, target, relationship) triple
    edge_duplicate_rel = GraphEdge(
        edge_id="edge_002",
        source_document_id="doc_b",
        target_document_id="doc_a",
        relationship=RelationshipType.AMENDS,
        evidence_text="Another sentence amending doc_a",
    )
    with pytest.raises(ValueError, match="Duplicate relationship"):
        collection.add_edge(edge_duplicate_rel)


def test_graph_edge_collection_corpus_validation():
    """Test validating edge document IDs against a registered known document corpus."""
    known_corpus = {"sebi_cir_2020_01", "sebi_cir_2024_01"}
    collection = GraphEdgeCollection(known_document_ids=known_corpus)

    valid_edge = GraphEdge(
        edge_id="edge_valid",
        source_document_id="sebi_cir_2024_01",
        target_document_id="sebi_cir_2020_01",
        relationship=RelationshipType.AMENDS,
        evidence_text="Amends circular",
    )
    collection.add_edge(valid_edge)
    assert len(collection) == 1

    # Edge with unknown source document ID
    unknown_source_edge = GraphEdge(
        edge_id="edge_unknown_src",
        source_document_id="unknown_doc_2025",
        target_document_id="sebi_cir_2020_01",
        relationship=RelationshipType.AMENDS,
        evidence_text="Amends circular",
    )
    with pytest.raises(ValueError, match="Unknown source_document_id"):
        collection.add_edge(unknown_source_edge)

    # Edge with unknown target document ID
    unknown_target_edge = GraphEdge(
        edge_id="edge_unknown_tgt",
        source_document_id="sebi_cir_2024_01",
        target_document_id="unknown_old_doc_2015",
        relationship=RelationshipType.AMENDS,
        evidence_text="Amends circular",
    )
    with pytest.raises(ValueError, match="Unknown target_document_id"):
        collection.add_edge(unknown_target_edge)


def test_graph_traversal_direction():
    """Verify outgoing (newer acting) and incoming (older affected) traversals."""
    collection = GraphEdgeCollection()

    # Circular C (2024) amends Circular B (2022) and references Circular A (2020)
    e1 = GraphEdge(
        edge_id="e1",
        source_document_id="doc_c",
        target_document_id="doc_b",
        relationship=RelationshipType.AMENDS,
        evidence_text="C amends B",
    )
    e2 = GraphEdge(
        edge_id="e2",
        source_document_id="doc_c",
        target_document_id="doc_a",
        relationship=RelationshipType.REFERENCES,
        evidence_text="C references A",
    )
    collection.add_edge(e1)
    collection.add_edge(e2)

    outgoing_c = collection.get_outgoing_edges("doc_c")
    assert len(outgoing_c) == 2

    incoming_b = collection.get_incoming_edges("doc_b")
    assert len(incoming_b) == 1
    assert incoming_b[0].source_document_id == "doc_c"
    assert incoming_b[0].relationship == RelationshipType.AMENDS

    incoming_a = collection.get_incoming_edges("doc_a")
    assert len(incoming_a) == 1
    assert incoming_a[0].relationship == RelationshipType.REFERENCES
