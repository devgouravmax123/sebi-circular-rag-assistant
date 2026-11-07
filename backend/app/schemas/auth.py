"""
Pydantic schemas for User Authentication and Identity (Milestone 8 - Task 8.2).
"""
from datetime import datetime
from typing import Optional
import uuid
import re

from pydantic import BaseModel, EmailStr, Field, field_validator, ConfigDict


class UserSignupRequest(BaseModel):
    """Request payload for user registration."""
    email: str = Field(..., description="User email address")
    password: str = Field(..., min_length=8, max_length=128, description="Plaintext password (min 8 chars)")
    full_name: Optional[str] = Field(None, max_length=255, description="Optional full name")

    @field_validator("email")
    @classmethod
    def validate_and_normalize_email(cls, v: str) -> str:
        s = v.strip().lower()
        if not s:
            raise ValueError("Email cannot be empty or whitespace.")
        # Basic email syntax validation
        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", s):
            raise ValueError("Invalid email format.")
        return s

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long.")
        if len(v.strip()) == 0:
            raise ValueError("Password cannot consist solely of whitespace.")
        return v


class UserLoginRequest(BaseModel):
    """Request payload for user authentication."""
    email: str = Field(..., description="User email address")
    password: str = Field(..., description="Plaintext password")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class UserResponse(BaseModel):
    """Safe public representation of a user identity (zero sensitive fields)."""
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: Optional[str] = None
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    """Bearer token response payload."""
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    user: UserResponse


class MessageResponse(BaseModel):
    """Generic status response payload."""
    message: str
