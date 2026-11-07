"""
Authentication and database session dependencies for FastAPI (Milestone 8 - Task 8.2).
"""
import logging
from typing import Generator, Optional
import uuid

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from app.core.config import Settings, get_settings
from app.core.auth import decode_access_token
from app.models.auth import UserModel

logger = logging.getLogger(__name__)

# Security scheme for Swagger UI & OpenAPI docs
http_bearer_scheme = HTTPBearer(auto_error=False)

# Module-level engine cache
_db_engine = None
_db_sessionmaker = None


def get_db_session(settings: Settings = Depends(get_settings)) -> Generator[Session, None, None]:
    """
    FastAPI dependency yielding a managed SQLAlchemy database session.
    Commits on success, rolls back on exception, and closes cleanly.
    """
    global _db_engine, _db_sessionmaker

    if not settings.is_database_enabled or not settings.database_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database is not configured on server.",
        )

    if _db_engine is None:
        _db_engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
        )
        _db_sessionmaker = sessionmaker(bind=_db_engine, expire_on_commit=False)

    session = _db_sessionmaker()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(http_bearer_scheme),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db_session),
) -> UserModel:
    """
    Reusable FastAPI dependency resolving the trusted authenticated user identity.
    Rejects missing, expired, invalid, or forged credentials with HTTP 401.
    Never trusts user IDs provided in request bodies or query parameters.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()

    try:
        payload = decode_access_token(token, settings)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError as e:
        logger.warning(f"Invalid access token rejected: {type(e).__name__}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token claims.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_uuid = uuid.UUID(user_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user identifier in token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Resolve authoritative user record from database
    user = db.query(UserModel).filter(UserModel.id == user_uuid).first()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User associated with token no longer exists.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is deactivated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


def get_db_session_optional(settings: Settings = Depends(get_settings)) -> Generator[Optional[Session], None, None]:
    """
    FastAPI dependency yielding a managed SQLAlchemy database session if configured.
    Yields None cleanly if database is not enabled/configured (ADR-020 zero-cost local default).
    """
    global _db_engine, _db_sessionmaker

    if not settings.is_database_enabled or not settings.database_url:
        yield None
        return

    if _db_engine is None:
        _db_engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
        )
        _db_sessionmaker = sessionmaker(bind=_db_engine, expire_on_commit=False)

    session = _db_sessionmaker()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(http_bearer_scheme),
    settings: Settings = Depends(get_settings),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> Optional[UserModel]:
    """
    Resolves the authenticated user if Bearer credentials are provided.
    If no credentials are provided (anonymous request), returns None without raising 401.
    If invalid/expired credentials are provided, raises 401 to prevent spoofed/broken credentials.
    """
    if credentials is None or not credentials.credentials:
        return None
    if db is None:
        return None
    return get_current_user(credentials=credentials, settings=settings, db=db)


