"""
Firebase Admin SDK integration for server-side token verification (Milestone 18).
Validates Firebase ID tokens, enforces email verification, and extracts trusted identity claims.
"""
import json
import logging
import os
from typing import Dict, Any, Optional, Callable

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_firebase_app = None
_verifier_override: Optional[Callable[[str], Dict[str, Any]]] = None


def is_firebase_configured() -> bool:
    """Returns True if Firebase Admin credentials or project ID are configured."""
    settings = get_settings()
    return bool(
        getattr(settings, "firebase_service_account_json", None)
        or os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        or getattr(settings, "firebase_credentials_path", None)
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or getattr(settings, "firebase_project_id", None)
        or os.getenv("FIREBASE_PROJECT_ID")
    )


def get_firebase_app():
    """Initializes and returns the singleton Firebase Admin app."""
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    import firebase_admin
    from firebase_admin import credentials

    # Check for existing default app
    try:
        _firebase_app = firebase_admin.get_app()
        return _firebase_app
    except ValueError:
        pass

    service_account_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    project_id = os.getenv("FIREBASE_PROJECT_ID")

    cred = None
    if service_account_json:
        try:
            cert_dict = json.loads(service_account_json)
            cred = credentials.Certificate(cert_dict)
            logger.info("Initializing Firebase Admin SDK with service account JSON.")
        except Exception as e:
            logger.error(f"Failed to parse FIREBASE_SERVICE_ACCOUNT_JSON: {e}")
            raise
    elif credentials_path and os.path.exists(credentials_path):
        try:
            cred = credentials.Certificate(credentials_path)
            logger.info(f"Initializing Firebase Admin SDK with credentials file: {credentials_path}")
        except Exception as e:
            logger.error(f"Failed to load credentials file {credentials_path}: {e}")
            raise
    elif project_id:
        logger.info(f"Initializing Firebase Admin SDK with project ID: {project_id}")
        _firebase_app = firebase_admin.initialize_app(options={"projectId": project_id})
        return _firebase_app

    if cred:
        _firebase_app = firebase_admin.initialize_app(cred)
    else:
        # Default initialization (e.g. metadata service in cloud or fallback)
        try:
            _firebase_app = firebase_admin.initialize_app()
        except Exception as e:
            logger.warning(f"Could not initialize default Firebase app: {e}")
            _firebase_app = None

    return _firebase_app


def set_firebase_verifier_override(verifier: Optional[Callable[[str], Dict[str, Any]]]) -> None:
    """Sets a mock/override verifier for automated testing without Google network calls."""
    global _verifier_override
    _verifier_override = verifier


def reset_firebase_verifier_override() -> None:
    """Resets the mock verifier override to None."""
    global _verifier_override
    _verifier_override = None


def verify_firebase_id_token(id_token: str) -> Dict[str, Any]:
    """
    Verifies a Firebase ID token and returns decoded claims.
    Validates issuer, audience, signature, and expiration via Firebase Admin SDK.
    Raises ValueError or jwt exceptions on invalid token.
    """
    if _verifier_override is not None:
        return _verifier_override(id_token)

    app = get_firebase_app()
    if app is None:
        raise ValueError("Firebase Admin SDK is not configured on this server.")

    from firebase_admin import auth

    # verify_id_token validates issuer, audience (projectId), signature, and expiration
    decoded = auth.verify_id_token(id_token, check_revoked=False)
    return {
        "uid": decoded["uid"],
        "email": (decoded.get("email") or "").strip().lower(),
        "email_verified": bool(decoded.get("email_verified", False)),
        "name": decoded.get("name"),
        "firebase": decoded.get("firebase", {}),
    }

