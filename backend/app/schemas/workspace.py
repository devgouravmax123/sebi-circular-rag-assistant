"""
Pydantic schemas for Workspaces, Memberships, and Collections (Milestone 8 - Task 8.3).
"""
from datetime import datetime
from typing import Optional, List
import uuid
import re

from pydantic import BaseModel, Field, field_validator, ConfigDict


# -------------------------------------------------------------------------
# Workspace Schemas
# -------------------------------------------------------------------------

class WorkspaceCreateRequest(BaseModel):
    """Payload to create a new user-owned workspace."""
    name: str = Field(..., min_length=2, max_length=100, description="Workspace display name")
    slug: str = Field(..., min_length=2, max_length=100, description="URL-safe workspace identifier")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Workspace name cannot be empty or whitespace.")
        return s

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        s = v.strip().lower()
        if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", s):
            raise ValueError("Slug must consist of lowercase alphanumeric characters and single hyphens.")
        if s in ("system", "admin", "api", "collections", "workspaces"):
            raise ValueError("Slug is reserved.")
        return s


class WorkspaceUpdateRequest(BaseModel):
    """Payload to update workspace metadata."""
    name: Optional[str] = Field(None, min_length=2, max_length=100, description="Updated workspace name")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            s = v.strip()
            if not s:
                raise ValueError("Workspace name cannot be empty or whitespace.")
            return s
        return v


class WorkspaceResponse(BaseModel):
    """Public representation of a workspace."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    is_system: bool
    owner_id: Optional[uuid.UUID] = None
    created_at: datetime
    updated_at: datetime
    user_role: Optional[str] = None


# -------------------------------------------------------------------------
# Membership Schemas
# -------------------------------------------------------------------------

class MembershipResponse(BaseModel):
    """Public representation of a workspace membership."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    user_email: Optional[str] = None
    user_full_name: Optional[str] = None
    role: str
    created_at: datetime
    updated_at: datetime


class MemberAddRequest(BaseModel):
    """Payload to add a user to a workspace."""
    email: str = Field(..., description="Email address of existing registered user")
    role: str = Field("member", description="Assigned role: admin, member, viewer")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        s = v.strip().lower()
        if not s:
            raise ValueError("Email cannot be empty.")
        return s

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        s = v.strip().lower()
        if s not in ("admin", "member", "viewer"):
            raise ValueError("Role must be one of: 'admin', 'member', 'viewer'.")
        return s


class MemberRoleUpdateRequest(BaseModel):
    """Payload to modify an existing member's role."""
    role: str = Field(..., description="Updated role: admin, member, viewer")

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        s = v.strip().lower()
        if s not in ("admin", "member", "viewer"):
            raise ValueError("Role must be one of: 'admin', 'member', 'viewer'.")
        return s


# -------------------------------------------------------------------------
# Collection Schemas
# -------------------------------------------------------------------------

class CollectionCreateRequest(BaseModel):
    """Payload to create a new user collection inside a workspace."""
    id: str = Field(..., min_length=2, max_length=100, description="Collection identifier")
    name: str = Field(..., min_length=2, max_length=255, description="Human-readable collection name")
    description: Optional[str] = Field(None, max_length=2000, description="Optional description")
    policy_name: str = Field("general_static", description="Grounding policy: 'general_static' or 'sebi_cscrf_strict'")
    enforce_regulatory_graph: bool = Field(False, description="Whether regulatory graph supersession is enforced")

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        s = v.strip().lower()
        if not re.match(r"^[a-z0-9_]+$", s):
            raise ValueError("Collection id must consist of lowercase alphanumeric characters and underscores.")
        if s in ("sebi_cscrf", "cisa_playbooks", "demo_finops", "general_synthetic", "system_legacy_jobs"):
            raise ValueError("Collection id is reserved by system collections.")
        return s

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Collection name cannot be empty.")
        return s

    @field_validator("policy_name")
    @classmethod
    def validate_policy_name(cls, v: str) -> str:
        s = v.strip().lower()
        if s not in ("general_static", "sebi_cscrf_strict"):
            raise ValueError("policy_name must be 'general_static' or 'sebi_cscrf_strict'.")
        return s


class CollectionUpdateRequest(BaseModel):
    """Payload to update collection metadata."""
    name: Optional[str] = Field(None, min_length=2, max_length=255, description="Updated collection name")
    description: Optional[str] = Field(None, max_length=2000, description="Updated description")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            s = v.strip()
            if not s:
                raise ValueError("Collection name cannot be empty.")
            return s
        return v


class CollectionDetailResponse(BaseModel):
    """Public representation of a collection."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    workspace_id: uuid.UUID
    name: str
    description: Optional[str] = None
    policy_name: str
    enforce_regulatory_graph: bool
    is_system: bool
    is_public: bool
    created_at: datetime
    updated_at: datetime

