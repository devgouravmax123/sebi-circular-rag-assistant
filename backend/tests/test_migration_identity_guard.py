"""Unit tests for fail-closed Alembic migration identity guard.

Verifies:
1. Unknown/ambiguous target: strictly rejected regardless of username or bypass flag.
2. Neon target (.neon.tech) + neondb_owner: allowed.
3. Neon target + non-owner (postgres, sebi_worker): rejected.
4. Neon target + bypass variable set: still strictly rejected.
5. Positively identified local PostgreSQL:
   - local + neondb_owner: allowed.
   - local + explicitly permitted non-owner (ALLOW_NON_OWNER_MIGRATIONS=true): allowed.
   - local + non-owner without bypass: rejected.
"""

import pytest

from app.core.migration_guard import verify_migration_identity


class TestMigrationIdentityGuard:
    # -------------------------------------------------------------------------
    # 1. Unknown / Ambiguous Targets (Fail-Closed)
    # -------------------------------------------------------------------------
    def test_unknown_target_with_neondb_owner_rejected(self):
        """Unknown target is rejected even if CURRENT_USER is neondb_owner."""
        with pytest.raises(RuntimeError, match="cannot be positively classified as Neon or approved local development"):
            verify_migration_identity(
                cur_user="neondb_owner",
                cur_db="custom_db",
                url_host="postgres.internal.corp",
                srv_addr="10.0.1.5",
                allow_non_owner_env=None,
            )

    def test_unknown_target_with_non_owner_rejected(self):
        """Unknown target is rejected for non-owner."""
        with pytest.raises(RuntimeError, match="cannot be positively classified as Neon or approved local development"):
            verify_migration_identity(
                cur_user="app_admin",
                cur_db="custom_db",
                url_host="postgres.internal.corp",
                srv_addr="10.0.1.5",
                allow_non_owner_env=None,
            )

    def test_unknown_target_with_bypass_flag_still_rejected(self):
        """Unknown target is rejected even if ALLOW_NON_OWNER_MIGRATIONS=true."""
        with pytest.raises(RuntimeError, match="cannot be positively classified as Neon or approved local development"):
            verify_migration_identity(
                cur_user="app_admin",
                cur_db="custom_db",
                url_host="postgres.internal.corp",
                srv_addr="10.0.1.5",
                allow_non_owner_env="true",
            )

    # -------------------------------------------------------------------------
    # 2. Neon Targets (.neon.tech)
    # -------------------------------------------------------------------------
    def test_neon_target_with_neondb_owner_allowed(self):
        """Neon target allows neondb_owner without exceptions."""
        verify_migration_identity(
            cur_user="neondb_owner",
            cur_db="neondb",
            url_host="ep-cool-fog-123456.us-east-2.aws.neon.tech",
            srv_addr="18.221.45.67",
            allow_non_owner_env=None,
        )

    def test_neon_target_with_non_owner_rejected(self):
        """Neon target strictly rejects any user other than neondb_owner."""
        with pytest.raises(RuntimeError, match="Neon migrations strictly require 'neondb_owner'"):
            verify_migration_identity(
                cur_user="postgres",
                cur_db="neondb",
                url_host="ep-cool-fog-123456.us-east-2.aws.neon.tech",
                srv_addr="18.221.45.67",
                allow_non_owner_env=None,
            )

    def test_neon_target_with_bypass_variable_still_rejected(self):
        """Bypass environment variable ALLOW_NON_OWNER_MIGRATIONS=true is strictly forbidden on Neon."""
        with pytest.raises(RuntimeError, match="Bypass variables are strictly forbidden on Neon"):
            verify_migration_identity(
                cur_user="sebi_worker",
                cur_db="neondb",
                url_host="ep-cool-fog-123456.us-east-2.aws.neon.tech",
                srv_addr="18.221.45.67",
                allow_non_owner_env="true",
            )

    # -------------------------------------------------------------------------
    # 3. Positively Identified Local Targets (localhost, 127.0.0.1, ::1, socket)
    # -------------------------------------------------------------------------
    def test_local_target_with_neondb_owner_allowed(self):
        """Local target allows neondb_owner unconditionally."""
        verify_migration_identity(
            cur_user="neondb_owner",
            cur_db="sebi_rag_dev",
            url_host="localhost",
            srv_addr="127.0.0.1",
            allow_non_owner_env=None,
        )

    def test_local_target_with_other_role_without_bypass_rejected(self):
        """Local target rejects non-owner when bypass is not enabled."""
        with pytest.raises(RuntimeError, match="Set ALLOW_NON_OWNER_MIGRATIONS=true to bypass"):
            verify_migration_identity(
                cur_user="postgres",
                cur_db="sebi_rag_dev",
                url_host="localhost",
                srv_addr="127.0.0.1",
                allow_non_owner_env=None,
            )

    def test_local_target_with_explicitly_permitted_non_owner_allowed(self):
        """Local target permits non-owner role when ALLOW_NON_OWNER_MIGRATIONS=true."""
        verify_migration_identity(
            cur_user="postgres",
            cur_db="sebi_rag_dev",
            url_host="localhost",
            srv_addr="127.0.0.1",
            allow_non_owner_env="true",
        )

    def test_local_socket_target_with_permitted_non_owner_allowed(self):
        """Local unix socket target (empty host, None srv_addr) permits non-owner with bypass."""
        verify_migration_identity(
            cur_user="runner",
            cur_db="sebi_rag_test",
            url_host="",
            srv_addr=None,
            allow_non_owner_env="1",
        )
