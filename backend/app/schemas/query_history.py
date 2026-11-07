"""
Pydantic schemas for persistent query history (Milestone 8 - Task 8.6).
"""
from datetime import datetime
from typing import List, Optional, Dict, Any
import uuid

from pydantic import BaseModel, Field, ConfigDict


class QueryHistorySummaryResponse(BaseModel):
    """Compact summary of a historical query for chronological list views."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique query history identifier.")
    collection_id: str = Field(..., description="Target collection identifier.")
    workspace_id: Optional[uuid.UUID] = Field(None, description="Owning workspace ID, if applicable.")
    query_text: str = Field(..., description="Original question text.")
    disposition: str = Field(..., description="Final answer disposition code.")
    verified_excerpts_count: int = Field(..., description="Count of verified excerpts returned.")
    technical_validation_passed: bool = Field(..., description="Whether technical grounding validation passed.")
    created_at: datetime = Field(..., description="Timestamp when query was executed.")


class QueryHistoryDetailResponse(BaseModel):
    """Detailed query history record including answer text, options, and citations."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(..., description="Unique query history identifier.")
    collection_id: str = Field(..., description="Target collection identifier.")
    workspace_id: Optional[uuid.UUID] = Field(None, description="Owning workspace ID, if applicable.")
    query_text: str = Field(..., description="Original question text.")
    query_options: Dict[str, Any] = Field(default_factory=dict, description="Execution parameters used.")
    disposition: str = Field(..., description="Final answer disposition code.")
    summary_narrative: Optional[str] = Field(None, description="Deterministic summary narrative.")
    answer_text: Optional[str] = Field(None, description="Synthesized answer text.")
    claim_citations: List[Dict[str, Any]] = Field(default_factory=list, description="Claim citations and evidence mappings.")
    verified_excerpts_count: int = Field(..., description="Count of verified excerpts returned.")
    technical_validation_passed: bool = Field(..., description="Whether technical grounding validation passed.")
    created_at: datetime = Field(..., description="Timestamp when query was executed.")


class QueryHistoryListResponse(BaseModel):
    """Paginated response containing query history summaries."""
    items: List[QueryHistorySummaryResponse]
    total: int
    limit: int
    offset: int

