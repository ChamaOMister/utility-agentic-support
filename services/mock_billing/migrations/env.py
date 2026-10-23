import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

VERSION_SCHEMA = "billing"


def _url() -> str:
    return (
        config.get_main_option("sqlalchemy.url") or os.environ["BILLING_DATABASE_URL"]
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        literal_binds=True,
        version_table_schema=VERSION_SCHEMA,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # billing_app cannot create objects in public, so the version table must live in billing.
        context.configure(connection=connection, version_table_schema=VERSION_SCHEMA)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
