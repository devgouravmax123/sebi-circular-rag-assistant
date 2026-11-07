"""harden_routine_privileges

Revision ID: 0008_harden_routine_privileges
Revises: 0007_add_query_history_ledger
Create Date: 2026-11-04 12:45:00.000000

Milestone 8 - Task 8.7: Routine Privilege Hardening & Least-Privilege Hygiene:
1. Explicitly revokes EXECUTE from PUBLIC on all 8 trigger functions in schema app
   created by migrations 0006 and 0007:
   - app.prevent_system_workspace_mutation()
   - app.maintain_workspace_owner_membership()
   - app.prevent_workspace_owner_transfer()
   - app.protect_workspace_owner_membership()
   - app.prevent_system_collection_mutation()
   - app.default_document_workspace_collection()
   - app.validate_job_document_consistency()
   - app.prevent_document_collection_drift()
2. Establishes global default privilege hardening for the migration creator role:
   `ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON ROUTINES FROM PUBLIC;`
   In PostgreSQL, per-schema default privileges are strictly additive to global
   defaults and cannot revoke permissions granted at the global default level (which
   grants EXECUTE to PUBLIC on routines). Global revocation for CURRENT_USER (neondb_owner)
   is necessary and sufficient to prevent newly created routines from receiving default
   PUBLIC execution authority. Furthermore, default privileges govern the creating role
   at execution time rather than any role assigned via ALTER FUNCTION ... OWNER TO.
3. Preserves worker least privilege: trigger functions remain callable strictly through
   trigger invocations, without granting direct execution authority to application roles.
4. Provides a fully reversible downgrade restoring standard PostgreSQL default routine grants
   (`ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON ROUTINES TO PUBLIC;`) and re-granting EXECUTE
   on the 8 trigger functions to PUBLIC.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0008_harden_routine_privileges"
down_revision: Union[str, None] = "0007_add_query_history_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Explicitly Revoke PUBLIC EXECUTE on Existing Trigger Functions in schema app
    # -------------------------------------------------------------------------
    op.execute(
        """
        REVOKE EXECUTE ON FUNCTION app.prevent_system_workspace_mutation() FROM PUBLIC;
        REVOKE EXECUTE ON FUNCTION app.maintain_workspace_owner_membership() FROM PUBLIC;
        REVOKE EXECUTE ON FUNCTION app.prevent_workspace_owner_transfer() FROM PUBLIC;
        REVOKE EXECUTE ON FUNCTION app.protect_workspace_owner_membership() FROM PUBLIC;
        REVOKE EXECUTE ON FUNCTION app.prevent_system_collection_mutation() FROM PUBLIC;
        REVOKE EXECUTE ON FUNCTION app.default_document_workspace_collection() FROM PUBLIC;
        REVOKE EXECUTE ON FUNCTION app.validate_job_document_consistency() FROM PUBLIC;
        REVOKE EXECUTE ON FUNCTION app.prevent_document_collection_drift() FROM PUBLIC;
        """
    )

    # -------------------------------------------------------------------------
    # 2. Establish Global Default Privileges for Migration Role
    # -------------------------------------------------------------------------
    # In PostgreSQL, schema-level default privileges are strictly additive to
    # global defaults and cannot revoke privileges granted globally. Because
    # PostgreSQL's built-in global default routine privilege grants EXECUTE to
    # PUBLIC, an ALTER DEFAULT PRIVILEGES IN SCHEMA ... REVOKE EXECUTE ON ROUTINES
    # is ineffective against new routines.
    # To ensure future routines created by the migration role (CURRENT_USER /
    # neondb_owner) do not inherit default PUBLIC EXECUTE, the revocation must be
    # issued globally for CURRENT_USER.
    # Furthermore, default privileges apply to the role executing CREATE FUNCTION
    # (CURRENT_USER), not roles to which ownership is transferred later.
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON ROUTINES FROM PUBLIC;
        """
    )


def downgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. Revert Global Default Privileges for Migration Role to PostgreSQL Standard
    # -------------------------------------------------------------------------
    # Restores PostgreSQL's built-in global default routine execution grant to PUBLIC
    # for routines subsequently created by CURRENT_USER.
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES GRANT EXECUTE ON ROUTINES TO PUBLIC;
        """
    )

    # -------------------------------------------------------------------------
    # 2. Restore Standard PostgreSQL Default EXECUTE to PUBLIC on Trigger Functions
    # -------------------------------------------------------------------------
    op.execute(
        """
        GRANT EXECUTE ON FUNCTION app.prevent_system_workspace_mutation() TO PUBLIC;
        GRANT EXECUTE ON FUNCTION app.maintain_workspace_owner_membership() TO PUBLIC;
        GRANT EXECUTE ON FUNCTION app.prevent_workspace_owner_transfer() TO PUBLIC;
        GRANT EXECUTE ON FUNCTION app.protect_workspace_owner_membership() TO PUBLIC;
        GRANT EXECUTE ON FUNCTION app.prevent_system_collection_mutation() TO PUBLIC;
        GRANT EXECUTE ON FUNCTION app.default_document_workspace_collection() TO PUBLIC;
        GRANT EXECUTE ON FUNCTION app.validate_job_document_consistency() TO PUBLIC;
        GRANT EXECUTE ON FUNCTION app.prevent_document_collection_drift() TO PUBLIC;
        """
    )
