"""Add optional Coach and Manager access to PED accounts.

Revision ID: 003_add_coach_manager_to_peds
Revises: 002_odyssey_extensions
"""

from alembic import op
import sqlalchemy as sa


revision = '003_add_coach_manager_to_peds'

down_revision = '002_odyssey_extensions'

branch_labels = None

depends_on = None


def upgrade() -> None:

    with op.batch_alter_table('peds') as batch:

        # =====================================================
        # COACH
        # =====================================================

        batch.add_column(
            sa.Column(
                'coach_name',
                sa.String(length=150),
                nullable=True,
            )
        )

        batch.add_column(
            sa.Column(
                'coach_email',
                sa.String(length=255),
                nullable=True,
            )
        )

        batch.add_column(
            sa.Column(
                'coach_contact_number',
                sa.String(length=20),
                nullable=True,
            )
        )

        # =====================================================
        # MANAGER
        # =====================================================

        batch.add_column(
            sa.Column(
                'manager_name',
                sa.String(length=150),
                nullable=True,
            )
        )

        batch.add_column(
            sa.Column(
                'manager_email',
                sa.String(length=255),
                nullable=True,
            )
        )

        batch.add_column(
            sa.Column(
                'manager_contact_number',
                sa.String(length=20),
                nullable=True,
            )
        )

        # =====================================================
        # INDEXES
        # =====================================================

        batch.create_index(
            'ix_peds_coach_email',
            ['coach_email'],
            unique=True,
        )

        batch.create_index(
            'ix_peds_manager_email',
            ['manager_email'],
            unique=True,
        )


def downgrade() -> None:

    with op.batch_alter_table('peds') as batch:

        batch.drop_index(
            'ix_peds_manager_email'
        )

        batch.drop_index(
            'ix_peds_coach_email'
        )

        batch.drop_column(
            'manager_contact_number'
        )

        batch.drop_column(
            'manager_email'
        )

        batch.drop_column(
            'manager_name'
        )

        batch.drop_column(
            'coach_contact_number'
        )

        batch.drop_column(
            'coach_email'
        )

        batch.drop_column(
            'coach_name'
        )