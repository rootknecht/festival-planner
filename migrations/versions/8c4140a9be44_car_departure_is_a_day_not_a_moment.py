"""car departure is a day, not a moment

Revision ID: 8c4140a9be44
Revises: b706e0b52b9f
Create Date: 2026-10-28 17:15:51.545067

A car's departure loses its clock time and becomes a plain date. An hour
agreed weeks in advance is fiction, and a wrong one is worse than none
because people plan around it; the exact time gets settled the night before,
and the meeting points carry the clock times that must be right.

Existing values are CARRIED OVER rather than dropped: whatever day each car
was leaving, it still leaves that day. Only the time of day is discarded,
which is the entire point of the change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c4140a9be44'
down_revision: Union[str, Sequence[str], None] = 'b706e0b52b9f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """datetime -> date, keeping the day."""
    op.add_column("car", sa.Column("departure_on", sa.Date(), nullable=True))
    # SQLite stores DATETIME as text, so the day is the first ten characters.
    # Done in SQL rather than by loading rows so the migration does not depend
    # on the application models, which have already moved on.
    op.execute(
        "UPDATE car SET departure_on = substr(departure_at, 1, 10) "
        "WHERE departure_at IS NOT NULL"
    )
    with op.batch_alter_table("car") as batch:
        batch.drop_column("departure_at")


def downgrade() -> None:
    """date -> datetime, at midnight.

    The original time of day cannot come back; it was deliberately discarded.
    Midnight is the honest reconstruction of "we leave this day".
    """
    op.add_column("car", sa.Column("departure_at", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE car SET departure_at = departure_on || ' 00:00:00' "
        "WHERE departure_on IS NOT NULL"
    )
    with op.batch_alter_table("car") as batch:
        batch.drop_column("departure_on")
