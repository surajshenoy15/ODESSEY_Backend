"""BNMIT Odyssey event/media/student certificate extensions.

Revision ID: 002_odyssey_extensions
Revises: 001_initial_schema
"""
from alembic import op
import sqlalchemy as sa

revision = '002_odyssey_extensions'
down_revision = '001_initial_schema'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('event_configs') as batch:
        batch.add_column(sa.Column('event_type', sa.String(length=30), nullable=False, server_default='SPORTS'))
        batch.add_column(sa.Column('description', sa.Text(), nullable=True))
        batch.add_column(sa.Column('poster_path', sa.String(length=500), nullable=True))
        batch.create_index('ix_event_configs_event_type', ['event_type'], unique=False)
    with op.batch_alter_table('students') as batch:
        batch.add_column(sa.Column('email', sa.String(length=255), nullable=True))
        batch.add_column(sa.Column('certificate_override', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column('certificate_override_reason', sa.Text(), nullable=True))
        batch.add_column(sa.Column('certificate_override_at', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('certificate_override_by', sa.String(length=36), nullable=True))
        batch.create_index('ix_students_email', ['email'], unique=False)
        batch.create_index('ix_students_certificate_override', ['certificate_override'], unique=False)
        batch.create_foreign_key('fk_students_certificate_override_by_admins', 'admins', ['certificate_override_by'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('students') as batch:
        batch.drop_constraint('fk_students_certificate_override_by_admins', type_='foreignkey')
        batch.drop_index('ix_students_certificate_override')
        batch.drop_index('ix_students_email')
        batch.drop_column('certificate_override_by')
        batch.drop_column('certificate_override_at')
        batch.drop_column('certificate_override_reason')
        batch.drop_column('certificate_override')
        batch.drop_column('email')
    with op.batch_alter_table('event_configs') as batch:
        batch.drop_index('ix_event_configs_event_type')
        batch.drop_column('poster_path')
        batch.drop_column('description')
        batch.drop_column('event_type')
