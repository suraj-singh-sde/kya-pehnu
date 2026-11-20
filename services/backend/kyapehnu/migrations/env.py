from alembic import context
from sqlalchemy import Connection
from sqlmodel import SQLModel

import kyapehnu.agent.models  # noqa: F401  these imports register the tables on SQLModel.metadata
import kyapehnu.auth.models  # noqa: F401
import kyapehnu.user.models  # noqa: F401
import kyapehnu.wardrobe.models  # noqa: F401

target_metadata = SQLModel.metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:  # CLI use (alembic revision --autogenerate): use the configured data dir
    from kyapehnu.core.config import get_settings
    from kyapehnu.core.db import make_engine

    with make_engine(get_settings().data_dir).connect() as conn:
        _run(conn)
