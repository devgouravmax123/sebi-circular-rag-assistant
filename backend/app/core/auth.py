"""
Authentication and password hashing services (Milestone 8 - Task 8.2).
Uses Argon2id for secure password hashing and PyJWT for signed bearer access tokens.
"""
from datetime import datetime, timezone, timedelta
import logging
from typing import Optional, Dict, Any
import uuid

import argon2
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
import jwt

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Standard Argon2id configuration (RFC 9106 recommended defaults for interactive login)
_password_hasher = argon2.PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=argon2.Type.ID,
)


def hash_password(password: str) -> str:
    """Hashes a plaintext password using Argon2id."""
    return _password_hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plaintext password against an Argon2id hash with constant-time verification."""
    try:
        return _password_hasher.verify(hashed_password, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception as e:
        logger.error(f"Unexpected password verification error: {type(e).__name__}")
        return False


def create_access_token(
    user_id: uuid.UUID,
    email: str,
    settings: Settings,
    expires_delta: Optional[timedelta] = None,
) -> tuple[str, int]:
    """
    Creates a cryptographically signed JWT access token.
    Returns (token_string, expires_in_seconds).
    """
    if expires_delta is not None:
        expire_time = datetime.now(timezone.utc) + expires_delta
        expires_seconds = int(expires_delta.total_seconds())
    else:
        expires_seconds = settings.auth_token_expire_minutes * 60
        expire_time = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)

    payload: Dict[str, Any] = {
        "sub": str(user_id),
        "email": email.strip().lower(),
        "iat": datetime.now(timezone.utc),
        "exp": expire_time,
        "jti": str(uuid.uuid4()),  # unique token id for audit / revocation tracking
    }

    secret_key = settings.get_auth_secret_key
    token = jwt.encode(payload, secret_key, algorithm=settings.auth_algorithm)
    return token, expires_seconds


def decode_access_token(token: str, settings: Settings) -> Dict[str, Any]:
    """
    Decodes and verifies a JWT access token against the configured signing algorithm and secret.
    Raises jwt.PyJWTError subclasses on invalid signature, expired token, or algorithm mismatch.
    """
    secret_key = settings.get_auth_secret_key
    # Enforce exact algorithm allowlist to prevent algorithm confusion attacks
    payload = jwt.decode(
        token,
        secret_key,
        algorithms=[settings.auth_algorithm],
        options={"require": ["sub", "exp", "iat"]},
    )
    return payload

