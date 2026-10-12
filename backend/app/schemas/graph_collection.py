from typing import Dict, List, Set, Optional
from app.schemas.graph import GraphEdge, RelationshipType


class GraphEdgeCollection:
    """In-memory collection and structural validator for supersession graph edges."""

    def __init__(
        self,
        edges: Optional[List[GraphEdge]] = None,
        known_document_ids: Optional[Set[str]] = None,
    ):
        self.known_document_ids: Set[str] = known_document_ids if known_document_ids is not None else set()
        self.edges: Dict[str, GraphEdge] = {}
        # Tracks unique (source, target, relationship) triples to prevent duplicate relationship edges
        self._relationship_triples: Set[tuple[str, str, RelationshipType]] = set()

        if edges:
            for e in edges:
                self.add_edge(e)

    def add_edge(self, edge: GraphEdge) -> None:
        """Add and structurally validate a graph edge."""
        if edge.edge_id in self.edges:
            raise ValueError(f"Duplicate edge_id: '{edge.edge_id}' already exists in collection")

        triple = (edge.source_document_id, edge.target_document_id, edge.relationship)
        if triple in self._relationship_triples:
            raise ValueError(
                f"Duplicate relationship: Edge between '{edge.source_document_id}' and '{edge.target_document_id}' "
                f"with type '{edge.relationship.value}' already exists"
            )

        # Validate against known document corpus if corpus is registered
        if self.known_document_ids:
            if edge.source_document_id not in self.known_document_ids:
                raise ValueError(
                    f"Unknown source_document_id: '{edge.source_document_id}' is not in known corpus"
                )
            if edge.target_document_id not in self.known_document_ids:
                raise ValueError(
                    f"Unknown target_document_id: '{edge.target_document_id}' is not in known corpus"
                )

        self.edges[edge.edge_id] = edge
        self._relationship_triples.add(triple)

    def get_outgoing_edges(self, source_id: str) -> List[GraphEdge]:
        """Return all edges acting FROM this newer circular."""
        return [e for e in self.edges.values() if e.source_document_id == source_id]

    def get_incoming_edges(self, target_id: str) -> List[GraphEdge]:
        """Return all edges acting ON this older circular."""
        return [e for e in self.edges.values() if e.target_document_id == target_id]

    def __len__(self) -> int:
        return len(self.edges)
