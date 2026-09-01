"""Conexão com o banco e sessão do SQLAlchemy."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import obter_config


def criar_engine(url: str | None = None) -> Engine:
    """Engine do SQLAlchemy, com o SQLite ajustado pra dois escritores.

    O garimpo roda numa tarefa de fundo enquanto a tela faz polling. Sem os
    ajustes abaixo isso dá `database is locked` na primeira rodada:

    - **WAL**: separa o log de escrita do arquivo principal, então quem lê
      não trava quem escreve. Sem isso o polling e o garimpo brigam.
    - **timeout**: em vez de falhar na hora, um escritor espera até 30s pela
      vez dele.
    - **foreign_keys**: o SQLite ignora chave estrangeira em silêncio se
      ninguém ligar o pragma à mão.
    """
    url = url or obter_config().database_url
    em_arquivo = url.startswith("sqlite") and ":memory:" not in url and url != "sqlite://"

    engine = create_engine(
        url,
        echo=False,
        future=True,
        connect_args={"timeout": 30} if em_arquivo else {},
    )

    if url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _ajustar_sqlite(conexao, _):  # noqa: ANN001
            cursor = conexao.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if em_arquivo:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


_engine: Engine | None = None
_Sessao: sessionmaker[Session] | None = None


def fabrica_de_sessao() -> sessionmaker[Session]:
    global _engine, _Sessao
    if _Sessao is None:
        _engine = criar_engine()
        _Sessao = sessionmaker(bind=_engine, expire_on_commit=False)
    return _Sessao


@contextmanager
def abrir_sessao() -> Iterator[Session]:
    """Abre uma sessão, comita no fim e desfaz tudo se der erro."""
    sessao = fabrica_de_sessao()()
    try:
        yield sessao
        sessao.commit()
    except Exception:
        sessao.rollback()
        raise
    finally:
        sessao.close()
