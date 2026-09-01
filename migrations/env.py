"""Ambiente do Alembic. A URL do banco vem da config, não do alembic.ini."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import event

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

    # DESLIGAR AS CHAVES ESTRANGEIRAS AQUI NÃO É OPCIONAL.
    #
    # O SQLite não sabe ALTER TABLE de verdade: pra mudar uma coluna, o
    # alembic (render_as_batch) recria a tabela — cria uma nova, copia,
    # **dropa a antiga** e renomeia. Com as FKs ligadas, esse DROP dispara
    # todo `ON DELETE CASCADE` que aponta pra ela e os filhos somem em
    # silêncio, sem erro nenhum. Foi o que a 0002 fez com as sementes,
    # descobertas, feedback, pontes e rodadas.
    #
    # Tem que ser por evento de conexão, e não `conexao.execute(...)`: um
    # execute abre transação implícita e o alembic deixa de gravar a versão.
    # O ouvinte roda depois do que o `criar_engine` registrou, então vence.
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _desligar_fk(conexao_dbapi, _):  # noqa: ANN001
            cursor = conexao_dbapi.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.close()

    with engine.connect() as conexao:
        context.configure(
            connection=conexao, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
