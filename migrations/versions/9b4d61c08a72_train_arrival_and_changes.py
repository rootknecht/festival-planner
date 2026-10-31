"""a train journey has an arrival station and changes

Revision ID: 9b4d61c08a72
Revises: 7a2e55b1c3d9
Create Date: 2026-10-31

The first cut of trains recorded only where the journey starts. Where it ends
is the other half of a connection, and a journey with a change is the normal
case outside an ICE line: "Hamburg -> Wacken, change at Itzehoe" is what
people actually book.

Changes are rows, not columns. Two or three of them happen, and
"change_1_station / change_1_time / change_2_station ..." is a schema that has
to grow every time somebody books a worse connection. The onward train number
sits on the change because that is what a change IS — one fact, not two.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9b4d61c08a72'
down_revision: Union[str, Sequence[str], None] = '7a2e55b1c3d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "train",
        sa.Column("arrival_station", sa.Text(), nullable=False, server_default=""),
    )
    op.create_table(
        "train_change",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("train_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("station", sa.Text(), nullable=False),
        sa.Column("change_at", sa.Time(), nullable=True),
        sa.Column("onward_number", sa.String(length=40), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["train_id"], ["train.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_train_change_train_id", "train_change", ["train_id"])


def downgrade() -> None:
    op.drop_index("ix_train_change_train_id", table_name="train_change")
    op.drop_table("train_change")
    op.drop_column("train", "arrival_station")
