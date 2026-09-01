"""Ambiente do Alembic. A URL do banco vem da config, não do alembic.ini."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.config import obter_config
from app.db import criar_engine
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=obter_config().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = criar_engine()
    with engine.connect() as conexao:
        # render_as_batch: o SQLite não sabe ALTER TABLE de verdade, então o
        # alembic recria a tabela quando precisa alterar coluna.
        context.configure(
            connection=conexao, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
