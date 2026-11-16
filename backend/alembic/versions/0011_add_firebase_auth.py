"""add_firebase_auth

Revision ID: 0011_add_firebase_auth
Revises: 0010_add_guest_sessions
Create Date: 2026-11-17 00:30:00.000000

Task: Firebase Authentication Integration (Milestone 18):
1. Adds firebase_uid column to public.users with unique index for Firebase identity resolution.
2. Adds email_verified boolean column to public.users.
3. Allows hashed_password column to be nullable for Firebase-authenticated users.
4. Preserves backward compatibility with legacy password-authenticated users.
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0011_add_firebase_auth"
down_revision: Union[str, None] = "0010_add_guest_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(
            """
            -- 1. Add firebase_uid to users for identity mapping
            ALTER TABLE public.users
                ADD COLUMN IF NOT EXISTS firebase_uid VARCHAR(128);

            -- 2. Create unique index on firebase_uid
            CREATE UNIQUE INDEX IF NOT EXISTS uq_users_firebase_uid
                ON public.users(firebase_uid)
                WHERE firebase_uid IS NOT NULL;

            -- 3. Add email_verified boolean
            ALTER TABLE public.users
                ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;

            -- 4. Allow hashed_password to be nullable for pure Firebase accounts
            ALTER TABLE public.users
                ALTER COLUMN hashed_password DROP NOT NULL;
            """
        )
    else:
        # SQLite / offline fallback for testing
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("firebase_uid", sa.String(128), nullable=True, unique=True))
            batch_op.add_column(sa.Column("email_verified", sa.Boolean(), nullable=False, server_default="0"))
            batch_op.alter_column("hashed_password", nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute(
            """
            DROP INDEX IF EXISTS public.uq_users_firebase_uid;
            ALTER TABLE public.users DROP COLUMN IF EXISTS email_verified;
            ALTER TABLE public.users DROP COLUMN IF EXISTS firebase_uid;
            ALTER TABLE public.users ALTER COLUMN hashed_password SET NOT NULL;
            """
        )
    else:
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("email_verified")
            batch_op.drop_column("firebase_uid")
            batch_op.alter_column("hashed_password", nullable=False)

