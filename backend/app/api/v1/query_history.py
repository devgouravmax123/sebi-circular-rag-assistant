"""
Query History API router implementing endpoints for persistent query history (Milestone 8 - Task 8.6).

Endpoints:
- GET /api/v1/query-history: Lists query history records for the authenticated user with pagination and filters.
- GET /api/v1/query-history/{history_id}: Retrieves a single query history record if owned by caller and accessible.
"""
import logging
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.api.deps import get_db_session, get_current_user
from app.models.auth import UserModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel
from app.models.query_history import QueryHistoryModel
from app.schemas.query_history import (
    QueryHistoryDetailResponse,
    QueryHistoryListResponse,
    QueryHistorySummaryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/query-history", tags=["query-history"])


@router.get(
    "",
    response_model=QueryHistoryListResponse,
    status_code=status.HTTP_200_OK,
    summary="List persistent query history records for the current user",
)
def list_query_history(
    workspace_id: Optional[uuid.UUID] = Query(None, description="Filter history by workspace ID."),
    collection_id: Optional[str] = Query(None, description="Filter history by collection ID."),
    limit: int = Query(20, ge=1, le=100, description="Page limit (1 to 100)."),
    offset: int = Query(0, ge=0, description="Page offset (>= 0)."),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> QueryHistoryListResponse:
    """
    Returns chronological paginated history records belonging to the authenticated user.
    Enforces that:
    1. Only records matching current_user.id are returned.
    2. Private collection history is excluded if the user's workspace membership has been revoked.
    3. If workspace_id filter is passed, verifies active membership or returns empty.
    """
    # Base query strictly filtered to current user
    q = db.query(QueryHistoryModel).filter(QueryHistoryModel.user_id == current_user.id)

    if workspace_id is not None:
        # Validate caller has active membership in workspace
        membership = (
            db.query(WorkspaceMembershipModel)
            .filter(
                WorkspaceMembershipModel.workspace_id == workspace_id,
                WorkspaceMembershipModel.user_id == current_user.id,
            )
            .first()
        )
        if not membership:
            # Return empty list if user is not member of requested workspace filter
            return QueryHistoryListResponse(items=[], total=0, limit=limit, offset=offset)
        q = q.filter(QueryHistoryModel.workspace_id == workspace_id)

    if collection_id is not None:
        cid = collection_id.strip()
        q = q.filter(func.lower(QueryHistoryModel.collection_id) == cid.lower())

    # Order newest first
    q = q.order_by(QueryHistoryModel.created_at.desc())

    # To ensure revoked memberships do not leak private historical queries,
    # we filter or verify membership on private workspaces
    # Fetch candidate records
    all_candidates = q.all()
    accessible_items = []

    # Cache user's active workspace memberships
    user_memberships = {
        m.workspace_id
        for m in db.query(WorkspaceMembershipModel.workspace_id)
        .filter(WorkspaceMembershipModel.user_id == current_user.id)
        .all()
    }

    # Cache public/system collections
    public_or_system_cols = {
        c.id.lower()
        for c in db.query(CollectionModel.id)
        .filter((CollectionModel.is_public == True) | (CollectionModel.is_system == True))
        .all()
    }

    for item in all_candidates:
        cid_lower = item.collection_id.lower()
        # Internal quarantine collection is never accessible
        if cid_lower == "system_legacy_jobs":
            continue

        # If it's a public/system collection, it's always accessible to its owner
        if cid_lower in public_or_system_cols:
            accessible_items.append(item)
            continue

        # For private collections, verify the user retains active membership in the workspace
        if item.workspace_id and item.workspace_id in user_memberships:
            accessible_items.append(item)

    total = len(accessible_items)
    page_slice = accessible_items[offset : offset + limit]

    return QueryHistoryListResponse(
        items=[QueryHistorySummaryResponse.model_validate(i) for i in page_slice],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{history_id}",
    response_model=QueryHistoryDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve single persistent query history record by ID",
)
def get_query_history_record(
    history_id: uuid.UUID,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> QueryHistoryDetailResponse:
    """
    Returns detailed history entry for history_id.
    Fails closed with generic 404 if record does not belong to user, does not exist,
    or belongs to a private workspace where caller's membership is no longer active.
    """
    record = (
        db.query(QueryHistoryModel)
        .filter(
            QueryHistoryModel.id == history_id,
            QueryHistoryModel.user_id == current_user.id,
        )
        .first()
    )
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Query history record not found.",
        )

    cid_lower = record.collection_id.lower()
    if cid_lower == "system_legacy_jobs":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Query history record not found.",
        )

    # Check if collection is private
    db_col = (
        db.query(CollectionModel)
        .filter(func.lower(CollectionModel.id) == cid_lower)
        .first()
    )
    if db_col and (not db_col.is_public and not db_col.is_system):
        # Verify active membership in workspace
        membership = (
            db.query(WorkspaceMembershipModel)
            .filter(
                WorkspaceMembershipModel.workspace_id == db_col.workspace_id,
                WorkspaceMembershipModel.user_id == current_user.id,
            )
            .first()
        )
        if not membership:
            logger.warning(
                f"Attempt to read private query history with revoked membership: user={current_user.id} record={record.id}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Query history record not found.",
            )

    return QueryHistoryDetailResponse.model_validate(record)

