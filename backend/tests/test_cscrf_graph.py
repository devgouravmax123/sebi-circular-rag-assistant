import json
import pytest
from pathlib import Path
from pydantic import ValidationError

from app.schemas.graph import (
    GraphEdge,
    RelationshipType,
    ScopeType,
    ConfidenceLevel,
    ReviewStatus,
)
from app.schemas.graph_collection import GraphEdgeCollection
from app.schemas.cscrf_graph_data import (
    VERIFIED_CSCRF_EDGES_DATA,
    create_verified_cscrf_graph,
    export_graph_to_json,
    load_graph_from_json,
)
from app.ingestion.corpus_registry import SEBI_CSCRF_CORPUS


def test_verified_cscrf_graph_initialization():
    """Verify that the official SEBI CSCRF verified graph initializes cleanly with 9 edges."""
    corpus_ids = {doc["document_id"] for doc in SEBI_CSCRF_CORPUS}
    assert len(corpus_ids) == 5

    graph = create_verified_cscrf_graph(known_document_ids=corpus_ids)
    assert len(graph) == 9
    assert len(graph.edges) == 9


def test_verified_cscrf_graph_referential_integrity():
    """Ensure all edges only connect documents in the 5-document SEBI CSCRF registered corpus."""
    corpus_ids = {doc["document_id"] for doc in SEBI_CSCRF_CORPUS}
    graph = create_verified_cscrf_graph(known_document_ids=corpus_ids)

    for edge in graph.edges.values():
        assert edge.source_document_id in corpus_ids
        assert edge.target_document_id in corpus_ids
        assert edge.source_document_id != edge.target_document_id


def test_references_never_induces_supersession():
    """Verify that REFERENCES relationships are informational and not supersession inducing."""
    corpus_ids = {doc["document_id"] for doc in SEBI_CSCRF_CORPUS}
    graph = create_verified_cscrf_graph(known_document_ids=corpus_ids)

    ref_edges = [e for e in graph.edges.values() if e.relationship == RelationshipType.REFERENCES]
    assert len(ref_edges) == 4  # 4 REFERENCES edges in the verified graph

    for edge in ref_edges:
        assert edge.is_supersession_inducing is False


def test_amends_is_supersession_inducing_and_partial():
    """Verify that all AMENDS edges induce supersession and specify affected provisions."""
    corpus_ids = {doc["document_id"] for doc in SEBI_CSCRF_CORPUS}
    graph = create_verified_cscrf_graph(known_document_ids=corpus_ids)

    amend_edges = [e for e in graph.edges.values() if e.relationship == RelationshipType.AMENDS]
    assert len(amend_edges) == 5  # 5 AMENDS edges

    for edge in amend_edges:
        assert edge.is_supersession_inducing is True
        assert edge.scope == ScopeType.PARTIAL
        assert len(edge.affected_provisions) > 0


def test_deterministic_serialization_and_loading(tmp_path: Path):
    """Test deterministic export to JSON and safe reloading."""
    corpus_ids = {doc["document_id"] for doc in SEBI_CSCRF_CORPUS}
    graph = create_verified_cscrf_graph(known_document_ids=corpus_ids)

    export_file = tmp_path / "cscrf_graph_test.json"
    export_graph_to_json(graph, export_file)
    assert export_file.exists()

    # Verify JSON structure
    with open(export_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list)
    assert len(data) == 9
    # Verify deterministic sorting by edge_id
    edge_ids = [item["edge_id"] for item in data]
    assert edge_ids == sorted(edge_ids)

    # Reload and assert identical state
    reloaded = load_graph_from_json(export_file, known_document_ids=corpus_ids)
    assert len(reloaded) == len(graph)
    for edge_id, edge in graph.edges.items():
        assert edge_id in reloaded.edges
        assert reloaded.edges[edge_id] == edge


def test_multihop_traversal_timeline_chain():
    """
    Test multi-hop traversal along the timeline extension chain:
    2025/096 -> AMENDS -> 2025/045 -> AMENDS -> 2024/113
    """
    corpus_ids = {doc["document_id"] for doc in SEBI_CSCRF_CORPUS}
    graph = create_verified_cscrf_graph(known_document_ids=corpus_ids)

    # Step 1: Outgoing edges from 2025/096
    out_96 = graph.get_outgoing_edges("sebi_cscrf_2025_096")
    amends_96_to_45 = [e for e in out_96 if e.target_document_id == "sebi_cscrf_2025_045" and e.relationship == RelationshipType.AMENDS]
    assert len(amends_96_to_45) == 1
    edge_96 = amends_96_to_45[0]
    assert edge_96.effective_date == "2025-06-30"
    assert "August 31, 2025" in edge_96.evidence_text
    assert "except Market Infrastructure Institutions (MIIs), KYC Registration Agencies (KRAs), and Qualified Registrars to an Issue and Share Transfer Agents (QRTAs)" in edge_96.evidence_text

    # Step 2: Outgoing edges from 2025/045
    out_45 = graph.get_outgoing_edges("sebi_cscrf_2025_045")
    amends_45_to_113 = [e for e in out_45 if e.target_document_id == "sebi_cscrf_2024_113" and e.relationship == RelationshipType.AMENDS]
    assert len(amends_45_to_113) == 1
    edge_45 = amends_45_to_113[0]
    assert edge_45.effective_date == "2025-03-28"
    assert "June 30, 2025" in edge_45.evidence_text

    # Step 3: Incoming edges on Master 2024/113
    inc_113 = graph.get_incoming_edges("sebi_cscrf_2024_113")
    assert len(inc_113) == 5  # Modified by 2025/045, 2025/060, 2025/119, and referenced by 2025/045, 2025/096


def test_circular_2025_096_explicit_exceptions():
    """Verify that circular 2025/096 evidence preserves specific entity exceptions for the August 31 deadline."""
    corpus_ids = {doc["document_id"] for doc in SEBI_CSCRF_CORPUS}
    graph = create_verified_cscrf_graph(known_document_ids=corpus_ids)

    edge_96 = graph.edges["edge_cscrf_2025_096_amends_2025_045_timelines_august"]
    # Evidence must explicitly document the exceptions
    assert "MIIs" in edge_96.evidence_text
    assert "KRAs" in edge_96.evidence_text
    assert "QRTAs" in edge_96.evidence_text
    assert "August 31, 2025" in edge_96.evidence_text
    assert edge_96.evidence_page == 2


def test_partial_amendments_preserve_unaffected_provisions():
    """Verify that partial amendments explicitly record targeted provisions without full invalidation."""
    corpus_ids = {doc["document_id"] for doc in SEBI_CSCRF_CORPUS}
    graph = create_verified_cscrf_graph(known_document_ids=corpus_ids)

    edge_119_tech = graph.edges["edge_cscrf_2025_119_amends_2024_113_technical_clarifications"]
    assert edge_119_tech.scope == ScopeType.PARTIAL
    # Ensure specific clauses are targeted
    assert any("ISO 27001" in p for p in edge_119_tech.affected_provisions)
    assert any("Zero-Trust" in p for p in edge_119_tech.affected_provisions)
    assert any("Two-Hour RTO" in p for p in edge_119_tech.affected_provisions)


def test_invalid_and_ambiguous_evidence_validation():
    """Synthetic test ensuring blank evidence, invalid dates, and corrupt inputs fail safely."""
    # Blank evidence rejected
    with pytest.raises(ValidationError):
        GraphEdge(
            edge_id="edge_blank_ev",
            source_document_id="sebi_cscrf_2025_045",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.AMENDS,
            scope=ScopeType.PARTIAL,
            affected_provisions=["Para 17"],
            evidence_text="   ",
        )

    # Missing affected_provisions with PARTIAL scope rejected
    with pytest.raises(ValidationError):
        GraphEdge(
            edge_id="edge_partial_empty",
            source_document_id="sebi_cscrf_2025_045",
            target_document_id="sebi_cscrf_2024_113",
            relationship=RelationshipType.AMENDS,
            scope=ScopeType.PARTIAL,
            affected_provisions=[],
            evidence_text="Amends something",
        )

    # Unknown document ID rejected by collection
    collection = GraphEdgeCollection(known_document_ids={"doc_a", "doc_b"})
    with pytest.raises(ValueError, match="Unknown source_document_id"):
        collection.add_edge(
            GraphEdge(
                edge_id="edge_unknown",
                source_document_id="unknown_doc",
                target_document_id="doc_a",
                relationship=RelationshipType.AMENDS,
                scope=ScopeType.PARTIAL,
                affected_provisions=["Section 1"],
                evidence_text="Some evidence",
            )
        )
