"""Initial BNMIT ODYSSEY schema.

Revision ID: 001_initial_schema
Revises: None
"""
from alembic import op
from app.core.database import Base
from app.models import entities  # noqa: F401

revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
