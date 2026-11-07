"""
backend/app/core/migration_guard.py
===================================
Enforces fail-closed identity invariants for Alembic migrations.
"""

from typing import Optional


def verify_migration_identity(
    cur_user: str,
    cur_db: str,
    url_host: Optional[str] = None,
    srv_addr: Optional[str] = None,
    allow_non_owner_env: Optional[str] = None,
) -> None:
    """
    Enforces fail-closed migration identity invariants:
    1. Target must be positively classified as either 'neon' (.neon.tech) or 'local' (localhost, 127.0.0.1, ::1, unix socket).
       Any ambiguous/unknown target is strictly rejected, regardless of username.
    2. Neon PostgreSQL targets (.neon.tech) strictly require cur_user == 'neondb_owner'.
       Environment bypasses are completely forbidden and ignored on Neon.
    3. Positively identified local PostgreSQL allows 'neondb_owner' unconditionally, and
       allows non-owner roles ONLY when allow_non_owner_env is explicitly ('true', '1').
    """
    host = (url_host or "").lower().strip()
    srv_ip = str(srv_addr).strip() if srv_addr is not None else None

    # Target classification
    is_neon = ".neon.tech" in host
    is_local_host = host in ("localhost", "127.0.0.1", "::1", "")
    is_local_ip = srv_ip in ("127.0.0.1", "::1", None)
    is_local = is_local_host and is_local_ip

    # Rule 1: Fail closed on unverified, ambiguous, or unknown targets
    if not is_neon and not is_local:
        raise RuntimeError(
            f"MIGRATION SECURITY ABORT: Target '{host}' cannot be positively classified as Neon or approved local development. "
            "Alembic migrations fail closed on ambiguous or unknown database targets."
        )

    # Rule 2: Neon target enforcement
    if is_neon:
        if cur_user != "neondb_owner":
            raise RuntimeError(
                f"MIGRATION SECURITY ABORT: Connection targets Neon PostgreSQL ({host}) as CURRENT_USER '{cur_user}'. "
                "Neon migrations strictly require 'neondb_owner' administrative identity. "
                "Bypass variables are strictly forbidden on Neon."
            )
        return

    # Rule 3: Local target enforcement
    if is_local:
        if cur_user == "neondb_owner":
            return
        bypass = (allow_non_owner_env or "").lower() in ("true", "1")
        if bypass:
            return  # Explicitly permitted under local development policy
        raise RuntimeError(
            f"MIGRATION SECURITY ABORT: Connected to local PostgreSQL as '{cur_user}', expected 'neondb_owner'. "
            "Set ALLOW_NON_OWNER_MIGRATIONS=true to bypass for verified local development."
        )
