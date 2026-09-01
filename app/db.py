"""Conexão com o banco e sessão do SQLAlchemy.

Fala os dois dialetos: SQLite (desenvolvimento, e produção com disco) e
Postgres (produção sem disco, como no plano gratuito do Render). Cada um
precisa de ajustes bem diferentes, e é isso que este arquivo resolve.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import obter_config


def normalizar_url(url: str) -> str:
    """Aceita as várias formas que os provedores entregam.

    - Render e Heroku dão `postgres://`, que o SQLAlchemy 2.x **recusa**
    - Neon e Supabase dão `postgresql://`, que procura o psycopg2 — e o
      projeto usa o psycopg 3

    As duas viram `postgresql+psycopg://`. Uma URL que já traz driver
    explícito passa intacta.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def e_sqlite_em_arquivo(url: str) -> bool:
    """SQLite gravando em disco — nem memória, nem outro banco."""
    return url.startswith("sqlite") and ":memory:" not in url and url != "sqlite://"


def criar_engine(url: str | None = None) -> Engine:
    url = normalizar_url(url or obter_config().database_url)

    if not url.startswith("sqlite"):
        # Postgres gerenciado (Neon, Supabase, Render) derruba conexão ociosa
        # sem avisar. Sem `pool_pre_ping` a primeira requisição depois de um
        # tempo parado morre com "server closed the connection".
        return create_engine(
            url,
            echo=False,
            future=True,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=5,
            max_overflow=2,
        )

    em_arquivo = e_sqlite_em_arquivo(url)
    engine = create_engine(
        url,
        echo=False,
        future=True,
        connect_args={"timeout": 30} if em_arquivo else {},
    )

    @event.listens_for(engine, "connect")
    def _ajustar_sqlite(conexao, _):  # noqa: ANN001
        """Três pragmas que o SQLite não liga sozinho.

        - **foreign_keys**: sem isso ele ignora chave estrangeira em silêncio
        - **WAL**: separa o log de escrita, pra quem lê não travar quem
          escreve. O garimpo escreve em segundo plano enquanto a tela faz
          polling; sem WAL isso dá `database is locked`
        - **timeout** (acima): em vez de falhar na hora, o escritor espera
        """
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
