"""a festival carries its own sending domain

Revision ID: 3f1c7a9d52e4
Revises: 6c0258f4be1a
Create Date: 2026-10-31

One instance serves several public domains on the same backend. Until now
every mail went out as the instance's single From address and every invite
link pointed at the single base URL, so a festival living on the second
domain still sent invitations that looked like they came from the first.

The column holds a DOMAIN, not an address: the From address for it comes from
the instance configuration (Settings.mail_brands), so rotating a sender
address is configuration rather than a data migration, and a domain removed
from the configuration degrades to the instance default instead of sending
from something this instance can no longer authenticate.

Empty string means "instance default", which is what every existing festival
gets — nobody's mail changes identity under them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '3f1c7a9d52e4'
down_revision: Union[str, Sequence[str], None] = '6c0258f4be1a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "festival",
        sa.Column("mail_domain", sa.String(length=255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("festival", "mail_domain")
