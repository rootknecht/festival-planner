"""trains as their own way of travelling

Revision ID: 7a2e55b1c3d9
Revises: 3f1c7a9d52e4
Create Date: 2026-10-31

Not a Car with a flag. A train has no driver, no colour, no pickups and no
route to draw, and above all no seat limit worth modelling — a group does not
run out of room on a train. Squeezing it into the car table would mean a
nullable column for every one of those differences and a seats_total nobody
means.

Times are nullable because a connection is worth sharing the moment the
number and the station are known; a required arrival time would stop people
entering what they already have.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7a2e55b1c3d9'
down_revision: Union[str, Sequence[str], None] = '3f1c7a9d52e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "train",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("festival_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("departure_station", sa.Text(), nullable=False, server_default=""),
        sa.Column("departure_on", sa.Date(), nullable=True),
        sa.Column("departure_time", sa.Time(), nullable=True),
        sa.Column("arrival_time", sa.Time(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["festival_id"], ["festival.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_train_festival_id", "train", ["festival_id"])

    op.create_table(
        "train_rider",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("train_id", sa.Integer(), nullable=False),
        sa.Column("participant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["train_id"], ["train.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["participant.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One person, one place on a given train: a double entry would make
        # the rider list lie about how many are travelling.
        sa.UniqueConstraint("train_id", "participant_id", name="uq_train_rider"),
    )
    op.create_index("ix_train_rider_train_id", "train_rider", ["train_id"])
    op.create_index("ix_train_rider_participant_id", "train_rider", ["participant_id"])


def downgrade() -> None:
    op.drop_index("ix_train_rider_participant_id", table_name="train_rider")
    op.drop_index("ix_train_rider_train_id", table_name="train_rider")
    op.drop_table("train_rider")
    op.drop_index("ix_train_festival_id", table_name="train")
    op.drop_table("train")
