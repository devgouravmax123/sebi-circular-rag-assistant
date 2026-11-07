"""
Workspace and Collection RBAC Authorization Dependencies (Milestone 8 - Task 8.3).
"""
import logging
from typing import Optional, Callable
import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db_session, get_current_user
from app.models.auth import UserModel, WorkspaceModel, WorkspaceMembershipModel
from app.models.collection import CollectionModel

logger = logging.getLogger(__name__)

SYSTEM_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-000000000000")


class WorkspaceAuthContext:
    """Carries verified workspace context and user membership."""
    def __init__(
        self,
        workspace: WorkspaceModel,
        membership: WorkspaceMembershipModel,
        user: UserModel,
    ):
        self.workspace = workspace
        self.membership = membership
        self.user = user

    @property
    def role(self) -> str:
        return self.membership.role

    @property
    def is_owner(self) -> bool:
        return self.membership.role == "owner"

    @property
    def is_admin(self) -> bool:
        return self.membership.role in ("owner", "admin")

    @property
    def is_member(self) -> bool:
        return self.membership.role in ("owner", "admin", "member")


def require_workspace_membership(
    workspace_id: uuid.UUID,
    user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db_session),
) -> WorkspaceAuthContext:
    """
    Validates that the authenticated user has an active membership in the specified workspace.
    Fails closed with 404 Not Found if workspace does not exist or user is not a member,
    preventing cross-tenant workspace enumeration.
    """
    # System workspace cannot be managed as an ordinary user workspace
    if workspace_id == SYSTEM_WORKSPACE_ID:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found.",
        )

    workspace = db.query(WorkspaceModel).filter(WorkspaceModel.id == workspace_id).first()
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found.",
        )

    membership = (
        db.query(WorkspaceMembershipModel)
        .filter(
            WorkspaceMembershipModel.workspace_id == workspace_id,
            WorkspaceMembershipModel.user_id == user.id,
        )
        .first()
    )

    if not membership:
        logger.warning(f"Unauthorized workspace access attempt: user={user.id} ws={workspace_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found.",
        )

    return WorkspaceAuthContext(workspace=workspace, membership=membership, user=user)


def require_workspace_role(allowed_roles: tuple[str, ...]) -> Callable[..., WorkspaceAuthContext]:
    """Factory creating dependency enforcing specific workspace RBAC roles."""
    def _role_checker(
        auth_ctx: WorkspaceAuthContext = Depends(require_workspace_membership),
    ) -> WorkspaceAuthContext:
        if auth_ctx.role not in allowed_roles:
            logger.warning(
                f"Forbidden workspace action: user={auth_ctx.user.id} role={auth_ctx.role} required={allowed_roles}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have sufficient permissions to perform this action.",
            )
        return auth_ctx

    return _role_checker


require_workspace_owner = require_workspace_role(("owner",))
require_workspace_admin = require_workspace_role(("owner", "admin"))
require_workspace_contributor = require_workspace_role(("owner", "admin", "member"))
require_workspace_reader = require_workspace_role(("owner", "admin", "member", "viewer"))

