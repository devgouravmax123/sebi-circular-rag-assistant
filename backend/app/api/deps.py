"""
Authentication and database session dependencies for FastAPI (Milestone 8 - Task 8.2).
"""
from datetime import datetime, timezone
import hashlib
import logging
from typing import Generator, Optional, Dict, Any
import uuid

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from app.core.config import Settings, get_settings
from app.core.auth import decode_access_token
from app.models.auth import UserModel
from app.models.guest import GuestSessionModel
from app.services.guest_service import _memory_guest_sessions, get_guest_workspace_id

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

    if payload.get("is_guest"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest sessions cannot access registered user resources. Please sign in or create an account.",
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
    If a valid guest token is provided, returns None (caller is a guest, not a UserModel).
    If invalid/expired credentials are provided, raises 401 to prevent spoofed/broken credentials.
    """
    if credentials is None or not credentials.credentials:
        return None

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

    if payload.get("is_guest"):
        # Guest identity is handled via get_current_guest_session_optional
        return None

    if db is None:
        return None

    return get_current_user(credentials=credentials, settings=settings, db=db)


def get_current_guest_session_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(http_bearer_scheme),
    settings: Settings = Depends(get_settings),
    db: Optional[Session] = Depends(get_db_session_optional),
) -> Optional[Dict[str, Any]]:
    """
    Resolves the guest session context if a valid guest Bearer token is provided.
    If an expired or deactivated guest token is provided, raises 401.
    If a registered user token or no credentials are provided, returns None cleanly.
    """
    if credentials is None or not credentials.credentials:
        return None

    token = credentials.credentials.strip()
    try:
        payload = decode_access_token(token, settings)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest session has expired (24-hour limit reached).",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        return None

    if not payload.get("is_guest"):
        return None

    session_id_str = payload.get("session_id")
    if not session_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed guest session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    guest_ws_id = get_guest_workspace_id(session_id_str)

    # 1. Database check if available
    if db is not None:
        try:
            sess_uuid = uuid.UUID(session_id_str)
            db_sess = (
                db.query(GuestSessionModel)
                .filter(
                    GuestSessionModel.id == sess_uuid,
                    GuestSessionModel.session_token_hash == token_hash,
                )
                .first()
            )
            if not db_sess:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Guest session not found or terminated.",
                )
            if not db_sess.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Guest session has been terminated.",
                )
            now_utc = datetime.now(timezone.utc)
            if db_sess.expires_at < now_utc:
                db_sess.is_active = False
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Guest session has expired (24-hour limit reached).",
                )
            return {
                "session_id": session_id_str,
                "workspace_id": guest_ws_id,
                "is_active": db_sess.is_active,
                "expires_at": db_sess.expires_at,
            }
        except (ValueError, HTTPException):
            raise
        except Exception as e:
            if db is not None:
                try:
                    db.rollback()
                except Exception:
                    pass
            logger.warning(f"Error resolving guest session in DB, falling back to memory: {e}")

    # 2. In-memory check
    mem_sess = _memory_guest_sessions.get(session_id_str)
    if not mem_sess:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest session not found or terminated.",
        )
    if not mem_sess.get("is_active"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest session has been terminated.",
        )
    now_utc = datetime.now(timezone.utc)
    if mem_sess.get("expires_at", now_utc) < now_utc:
        mem_sess["is_active"] = False
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Guest session has expired (24-hour limit reached).",
        )
    return {
        "session_id": session_id_str,
        "workspace_id": guest_ws_id,
        "is_active": True,
        "expires_at": mem_sess.get("expires_at"),
    }
