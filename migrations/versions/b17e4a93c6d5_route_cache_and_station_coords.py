"""road route cache, and coordinates for train stations

Revision ID: b17e4a93c6d5
Revises: 9b4d61c08a72
Create Date: 2026-10-31

The map drew straight dashed lines because the only free routing engine
reachable without terms trouble needs an API key. With a key configured, a
car's line follows the road; the geometry is cached per rounded waypoint list
so that a page view never spends a request from the daily allowance.

Train stations were free text with no coordinates, so trains had no presence
on the map at all. They are geocoded on save now, like a car's departure
address, and the journey is drawn as station to change to station.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b17e4a93c6d5'
down_revision: Union[str, Sequence[str], None] = '9b4d61c08a72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "route_cache",
        sa.Column("route_hash", sa.String(length=64), nullable=False),
        sa.Column("profile", sa.String(length=40), nullable=False, server_default="driving-car"),
        sa.Column("waypoints", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("geometry", sa.Text(), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("route_hash"),
    )
    for column in ("departure_lat", "departure_lon", "arrival_lat", "arrival_lon"):
        op.add_column("train", sa.Column(column, sa.Float(), nullable=True))
    for column in ("lat", "lon"):
        op.add_column("train_change", sa.Column(column, sa.Float(), nullable=True))


def downgrade() -> None:
    for column in ("lon", "lat"):
        op.drop_column("train_change", column)
    for column in ("arrival_lon", "arrival_lat", "departure_lon", "departure_lat"):
        op.drop_column("train", column)
    op.drop_table("route_cache")
