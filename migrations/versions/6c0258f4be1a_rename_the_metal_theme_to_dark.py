"""rename the metal theme to dark

Revision ID: 6c0258f4be1a
Revises: 8c4140a9be44
Create Date: 2026-10-29 13:06:14.110224

The old "metal" theme was a plain dark interface with a red accent; the name
promised something the design never delivered. It is now called "dark", and
"metal" belongs to a new theme that actually looks the part.

Every festival already set to "metal" moves to "dark", so nobody's chosen
appearance changes under them: the stylesheet they were loading is the one
they keep loading, under its honest name. The new metal can be picked in
settings by anyone who wants it.

Pure data. The column is a plain string, so ADDING a theme needs no migration
at all — only the rename does.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '6c0258f4be1a'
down_revision: Union[str, Sequence[str], None] = '8c4140a9be44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE festival SET theme = 'dark' WHERE theme = 'metal'")


def downgrade() -> None:
    """Back to the old name.

    A festival that deliberately picked the NEW metal theme is
    indistinguishable here from one migrated up, so both end as 'metal' —
    which is the only theme of that name once the new CSS is gone.
    """
    op.execute("UPDATE festival SET theme = 'metal' WHERE theme = 'dark'")
