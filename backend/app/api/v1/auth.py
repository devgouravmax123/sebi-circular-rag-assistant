"""
Authentication REST API endpoints for user registration, login, logout, and identity inspection.
Path prefix: /api/v1/auth
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

from app.core.config import Settings, get_settings
from app.core.auth import hash_password, verify_password, create_access_token
from app.api.deps import get_db_session, get_current_user
from app.models.auth import UserModel
from app.schemas.auth import (
    UserSignupRequest,
    UserLoginRequest,
    UserResponse,
    TokenResponse,
    MessageResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post(
    "/signup",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
)
def signup(
    payload: UserSignupRequest,
    db: Session = Depends(get_db_session),
) -> UserResponse:
    """
    Registers a new user account with normalized email and Argon2id password hashing.
    Enforces uniqueness against lower(email).
    """
    normalized_email = payload.email.strip().lower()

    # Pre-check for duplicate user to return clean client-facing conflict error
    existing = db.query(UserModel).filter(func.lower(UserModel.email) == normalized_email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email address already exists.",
        )

    hashed_pw = hash_password(payload.password)
    user = UserModel(
        email=normalized_email,
        hashed_password=hashed_pw,
        full_name=payload.full_name.strip() if payload.full_name else None,
        is_active=True,
    )

    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email address already exists.",
        )

    logger.info(f"User signed up successfully: {user.id}")
    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate and receive bearer access token",
)
def login(
    payload: UserLoginRequest,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db_session),
) -> TokenResponse:
    """
    Authenticates user credentials against the stored Argon2id hash.
    Returns generic HTTP 401 for both non-existent emails and wrong passwords
    to prevent user enumeration.
    """
    normalized_email = payload.email.strip().lower()
    user = db.query(UserModel).filter(func.lower(UserModel.email) == normalized_email).first()

    # Generic failure response helper
    def _generic_failure():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user:
        # Perform dummy verification to mitigate timing attacks against non-existent accounts
        _ = verify_password(payload.password, "$argon2id$v=19$m=65536,t=3,p=4$dummy$dummy")
        _generic_failure()

    if not user.is_active:
        logger.warning(f"Login attempt on deactivated account: {user.id}")
        _generic_failure()

    if not verify_password(payload.password, user.hashed_password):
        logger.warning(f"Failed password verification for user: {user.id}")
        _generic_failure()

    access_token, expires_in_seconds = create_access_token(
        user_id=user.id,
        email=user.email,
        settings=settings,
    )

    logger.info(f"User authenticated successfully: {user.id}")
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in_seconds=expires_in_seconds,
        user=UserResponse.model_validate(user),
    )


@router.post(
    "/logout",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Terminate session / logout",
)
def logout(
    current_user: UserModel = Depends(get_current_user),
) -> MessageResponse:
    """
    Terminates the client session.
    For stateless signed bearer tokens, the client discards the token.
    Returns standard confirmation response without exposing token or internal state.
    """
    logger.info(f"User logged out: {current_user.id}")
    return MessageResponse(message="Successfully logged out.")


@router.get(
    "/me",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve current authenticated user identity",
)
def get_me(
    current_user: UserModel = Depends(get_current_user),
) -> UserResponse:
    """
    Returns the trusted identity of the currently authenticated user.
    Strictly protected by bearer token authentication.
    """
    return UserResponse.model_validate(current_user)

