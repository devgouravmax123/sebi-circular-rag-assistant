"""
REST API routes for Workspace and Membership management (Milestone 8 - Task 8.3).
Path prefix: /api/v1/workspaces
"""
import logging
from typing import List
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

from app.api.deps import get_db_session, get_current_user
from app.api.workspace_deps import (
    WorkspaceAuthContext,
    require_workspace_reader,
    require_workspace_admin,
    require_workspace_owner,
)
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.schemas.workspace import (
    WorkspaceCreateRequest,
    WorkspaceUpdateRequest,
    WorkspaceResponse,
    MembershipResponse,
    MemberAddRequest,
    MemberRoleUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


# -------------------------------------------------------------------------
# Workspace Endpoints
# -------------------------------------------------------------------------

@router.post(
    "",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new private workspace",
)
def create_workspace(
    payload: WorkspaceCreateRequest,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> WorkspaceResponse:
    """
    Creates a new user-owned workspace.
    The creator is atomically assigned as the workspace owner via database trigger.
    """
    # Check slug availability
    existing_slug = db.query(WorkspaceModel).filter(func.lower(WorkspaceModel.slug) == payload.slug).first()
    if existing_slug:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A workspace with this slug already exists.",
        )

    ws = WorkspaceModel(
        name=payload.name,
        slug=payload.slug,
        is_system=False,
        owner_id=user.id,
    )

    db.add(ws)
    try:
        db.commit()
        db.refresh(ws)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A workspace with this slug already exists.",
        )

    resp = WorkspaceResponse.model_validate(ws)
    resp.user_role = "owner"
    logger.info(f"Workspace created: {ws.id} slug={ws.slug} by user={user.id}")
    return resp


@router.get(
    "",
    response_model=List[WorkspaceResponse],
    status_code=status.HTTP_200_OK,
    summary="List workspaces where current user has membership",
)
def list_my_workspaces(
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> List[WorkspaceResponse]:
    """
    Returns only workspaces where the authenticated user is an active member.
    Never exposes other tenants' private workspaces.
    """
    memberships = (
        db.query(WorkspaceMembershipModel)
        .join(WorkspaceModel, WorkspaceMembershipModel.workspace_id == WorkspaceModel.id)
        .filter(
            WorkspaceMembershipModel.user_id == user.id,
            WorkspaceModel.is_system == False,
        )
        .all()
    )

    results: List[WorkspaceResponse] = []
    for m in memberships:
        ws_resp = WorkspaceResponse.model_validate(m.workspace)
        ws_resp.user_role = m.role
        results.append(ws_resp)

    return results


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_200_OK,
    summary="Get workspace details",
)
def get_workspace(
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_reader),
) -> WorkspaceResponse:
    """Returns workspace metadata for an authorized member."""
    resp = WorkspaceResponse.model_validate(auth_ctx.workspace)
    resp.user_role = auth_ctx.role
    return resp


@router.patch(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_200_OK,
    summary="Update workspace metadata",
)
def update_workspace(
    payload: WorkspaceUpdateRequest,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_admin),
    db: Session = Depends(get_db_session),
) -> WorkspaceResponse:
    """
    Updates workspace metadata (name only).
    Requires owner or admin role. Ownership and slug transfers are prohibited.
    """
    if payload.name is not None:
        auth_ctx.workspace.name = payload.name

    db.commit()
    db.refresh(auth_ctx.workspace)

    resp = WorkspaceResponse.model_validate(auth_ctx.workspace)
    resp.user_role = auth_ctx.role
    return resp


# -------------------------------------------------------------------------
# Membership Endpoints
# -------------------------------------------------------------------------

@router.get(
    "/{workspace_id}/members",
    response_model=List[MembershipResponse],
    status_code=status.HTTP_200_OK,
    summary="List members of an authorized workspace",
)
def list_workspace_members(
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_reader),
    db: Session = Depends(get_db_session),
) -> List[MembershipResponse]:
    """Lists all members and their roles in the workspace."""
    memberships = (
        db.query(WorkspaceMembershipModel)
        .filter(WorkspaceMembershipModel.workspace_id == auth_ctx.workspace.id)
        .order_by(WorkspaceMembershipModel.created_at.asc())
        .all()
    )

    responses: List[MembershipResponse] = []
    for m in memberships:
        resp = MembershipResponse.model_validate(m)
        resp.user_email = m.user.email if m.user else None
        resp.user_full_name = m.user.full_name if m.user else None
        responses.append(resp)

    return responses


@router.post(
    "/{workspace_id}/members",
    response_model=MembershipResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add an existing user to workspace",
)
def add_workspace_member(
    payload: MemberAddRequest,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_admin),
    db: Session = Depends(get_db_session),
) -> MembershipResponse:
    """
    Adds a registered user to the workspace.
    Requires admin or owner role. Owner role cannot be granted through this endpoint.
    """
    target_user = db.query(UserModel).filter(func.lower(UserModel.email) == payload.email).first()
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User with this email was not found.",
        )

    existing_mem = (
        db.query(WorkspaceMembershipModel)
        .filter(
            WorkspaceMembershipModel.workspace_id == auth_ctx.workspace.id,
            WorkspaceMembershipModel.user_id == target_user.id,
        )
        .first()
    )
    if existing_mem:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this workspace.",
        )

    new_mem = WorkspaceMembershipModel(
        workspace_id=auth_ctx.workspace.id,
        user_id=target_user.id,
        role=payload.role,
    )
    db.add(new_mem)
    db.commit()
    db.refresh(new_mem)

    resp = MembershipResponse.model_validate(new_mem)
    resp.user_email = target_user.email
    resp.user_full_name = target_user.full_name
    logger.info(f"Member added to ws={auth_ctx.workspace.id}: user={target_user.id} role={payload.role}")
    return resp


@router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=MembershipResponse,
    status_code=status.HTTP_200_OK,
    summary="Update a workspace member's role",
)
def update_member_role(
    user_id: uuid.UUID,
    payload: MemberRoleUpdateRequest,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_owner),
    db: Session = Depends(get_db_session),
) -> MembershipResponse:
    """
    Modifies a member's role. Strictly requires workspace owner.
    Prevents self-demotion or modifying the workspace owner.
    """
    if user_id == auth_ctx.user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workspace owner cannot modify their own role.",
        )

    mem = (
        db.query(WorkspaceMembershipModel)
        .filter(
            WorkspaceMembershipModel.workspace_id == auth_ctx.workspace.id,
            WorkspaceMembershipModel.user_id == user_id,
        )
        .first()
    )
    if not mem:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in workspace.",
        )

    mem.role = payload.role
    db.commit()
    db.refresh(mem)

    resp = MembershipResponse.model_validate(mem)
    resp.user_email = mem.user.email if mem.user else None
    resp.user_full_name = mem.user.full_name if mem.user else None
    return resp


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member from workspace",
)
def remove_workspace_member(
    user_id: uuid.UUID,
    auth_ctx: WorkspaceAuthContext = Depends(require_workspace_admin),
    db: Session = Depends(get_db_session),
) -> None:
    """
    Removes a member from workspace.
    Admins can remove non-admin/member users; owners can remove anyone except themselves.
    The primary owner can never be removed.
    """
    if user_id == auth_ctx.workspace.owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The workspace owner cannot be removed from the workspace.",
        )

    mem = (
        db.query(WorkspaceMembershipModel)
        .filter(
            WorkspaceMembershipModel.workspace_id == auth_ctx.workspace.id,
            WorkspaceMembershipModel.user_id == user_id,
        )
        .first()
    )
    if not mem:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found in workspace.",
        )

    # Admins cannot remove other admins or owners
    if auth_ctx.role == "admin" and mem.role in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admins cannot remove other admins or owners.",
        )

    db.delete(mem)
    db.commit()
    logger.info(f"Member {user_id} removed from workspace {auth_ctx.workspace.id}")
    return None

