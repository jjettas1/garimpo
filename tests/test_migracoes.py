"""Testes das migrations.

Existe um teste aqui porque a migration 0002 apagou dados de verdade: no
SQLite, alterar uma coluna recria a tabela, e o DROP da antiga dispara os
ON DELETE CASCADE dos filhos.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent


def _alembic(comando: str, banco: Path) -> None:
    ambiente = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{banco}",
        "LASTFM_API_KEY": "chave-de-teste",
    }
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", comando]
        if not comando.startswith("down:")
        else [sys.executable, "-m", "alembic", "downgrade", comando[5:]],
        cwd=RAIZ,
        env=ambiente,
        capture_output=True,
        text=True,
    )
    assert resultado.returncode == 0, resultado.stderr


@pytest.fixture
def banco(tmp_path):
    return tmp_path / "migracao.db"


def _povoar(banco: Path) -> None:
    """Um usuário com um filho em cada tabela que aponta pra ele."""
    import sqlite3

    c = sqlite3.connect(banco)
    c.executescript(
        """
        INSERT INTO users (id, email, password_hash, email_verificado, criado_em,
                           piso_ouvintes, teto_ouvintes)
             VALUES (1, 'a@b.com', 'x', 1, '2026-01-01', 1000, 200000);
        INSERT INTO artists (id, nome, nome_norm, listeners, atualizado_em)
             VALUES (1, 'Alvo', 'alvo', 5000, '2026-01-01'),
                    (2, 'Ponte', 'ponte', 90000, '2026-01-01');
        INSERT INTO seed_artists (user_id, nome, ordem) VALUES (1, 'Duster', 0);
        INSERT INTO discoveries (id, user_id, artist_id, ponte_id, rota_json, score,
                                 afinidade, raridade, coerencia, n_rotas, semana, criado_em)
             VALUES (1, 1, 1, 2, '[]', 0.3, 0.8, 0.4, 0.6, 1, '2026-W36', '2026-01-01');
        INSERT INTO feedback (discovery_id, veredito, criado_em)
             VALUES (1, 'aprovado', '2026-01-01');
        INSERT INTO bridge_stats (user_id, artist_id, aprovados, total) VALUES (1, 2, 1, 1);
        INSERT INTO runs (user_id, status, iniciado_em) VALUES (1, 'pronto', '2026-01-01');
        """
    )
    c.commit()
    c.close()


def _contar(banco: Path) -> dict[str, int]:
    import sqlite3

    c = sqlite3.connect(banco)
    tabelas = ("seed_artists", "discoveries", "feedback", "bridge_stats", "runs", "users")
    contagem = {t: c.execute(f"select count(*) from {t}").fetchone()[0] for t in tabelas}
    c.close()
    return contagem


def test_migration_nao_apaga_os_filhos_do_usuario(banco):
    """A 0002 mexe na tabela `users`, e no SQLite isso significa recriá-la.

    Com as chaves estrangeiras ligadas, o DROP da tabela antiga dispara todo
    `ON DELETE CASCADE` que aponta pra ela e apaga sementes, descobertas,
    feedback, pontes e rodadas — em silêncio, sem erro nenhum.
    """
    _alembic("0001", banco)
    _povoar(banco)
    antes = _contar(banco)
    assert all(n == 1 for n in antes.values()), f"o povoamento falhou: {antes}"

    _alembic("head", banco)

    depois = _contar(banco)
    assert depois == antes, f"a migration comeu dados: antes={antes} depois={depois}"


def test_ida_e_volta_preserva_os_dados(banco):
    _alembic("0001", banco)
    _povoar(banco)
    antes = _contar(banco)

    _alembic("head", banco)
    _alembic("down:0001", banco)
    _alembic("head", banco)

    assert _contar(banco) == antes


def test_migration_chega_no_schema_dos_models(banco):
    """A migration e os models têm que descrever a mesma coisa."""
    from sqlalchemy import create_engine, inspect

    from app.models import Base

    _alembic("head", banco)
    do_alembic = create_engine(f"sqlite:///{banco}")

    dos_models = create_engine("sqlite://")
    Base.metadata.create_all(dos_models)

    ia, im = inspect(do_alembic), inspect(dos_models)
    assert set(ia.get_table_names()) - {"alembic_version"} == set(im.get_table_names())
    for tabela in im.get_table_names():
        assert {c["name"] for c in ia.get_columns(tabela)} == {
            c["name"] for c in im.get_columns(tabela)
        }, f"colunas diferentes em {tabela}"


# ------------------------------------------------------------ os dois dialetos

# O app roda em SQLite (dev, e prod com disco) e em Postgres (prod sem disco).
# Criar um engine não conecta em nada, então dá pra conferir a configuração
# dos dois sem ter um Postgres à mão.


@pytest.mark.parametrize(
    "entregue,esperado",
    [
        # Render e Heroku dão isto, e o SQLAlchemy 2.x recusa `postgres://`
        ("postgres://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        # Neon e Supabase dão isto, que procuraria o psycopg2
        ("postgresql://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        ("postgresql://u:p@host/db?sslmode=require", "postgresql+psycopg://u:p@host/db?sslmode=require"),
        # já com driver: passa intacta
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        # sqlite não é tocado
        ("sqlite:///garimpo.db", "sqlite:///garimpo.db"),
        ("sqlite://", "sqlite://"),
    ],
)
def test_url_do_provedor_vira_url_que_o_sqlalchemy_aceita(entregue, esperado):
    from app.db import normalizar_url

    assert normalizar_url(entregue) == esperado


def test_engine_de_postgres_liga_o_pre_ping():
    """Postgres gerenciado derruba conexão ociosa sem avisar. Sem pre_ping a
    primeira requisição depois de um tempo parado morre."""
    from app.db import criar_engine

    engine = criar_engine("postgresql+psycopg://u:p@host/db")

    assert engine.dialect.name == "postgresql"
    assert engine.pool._pre_ping is True


def test_engine_de_postgres_nao_leva_config_de_sqlite():
    """`timeout` e os PRAGMA são remendos do SQLite; no Postgres quebrariam."""
    from app.db import criar_engine

    engine = criar_engine("postgresql+psycopg://u:p@host/db")

    assert "timeout" not in engine.dialect.create_connect_args(engine.url)[1]


@pytest.mark.parametrize(
    "url,em_arquivo",
    [
        ("sqlite:///garimpo.db", True),
        ("sqlite:////var/data/garimpo.db", True),
        ("sqlite://", False),
        ("sqlite:///:memory:", False),
        ("postgresql+psycopg://u:p@host/db", False),
    ],
)
def test_reconhece_sqlite_em_disco(url, em_arquivo):
    """Só o SQLite em disco precisa de WAL e timeout."""
    from app.db import e_sqlite_em_arquivo

    assert e_sqlite_em_arquivo(url) is em_arquivo


def test_o_driver_do_postgres_esta_instalado():
    """Sem ele o deploy quebra na primeira conexão, não no build."""
    import psycopg  # noqa: F401


# ------------------------------------------------------ contra um postgres real

POSTGRES_DE_TESTE = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(
    not POSTGRES_DE_TESTE,
    reason="defina TEST_DATABASE_URL com a url de um postgres descartável",
)
def test_migrations_e_modelos_funcionam_no_postgres():
    """O teste que fecha a conta: roda as migrations num Postgres de verdade
    e exercita o modelo inteiro.

        TEST_DATABASE_URL='postgresql://...' pytest tests/test_migracoes.py

    Ele **apaga e recria** o schema, então aponte pra um banco descartável.
    """
    if not POSTGRES_DE_TESTE.startswith(("postgres://", "postgresql://", "postgresql+")):
        pytest.fail(
            f"TEST_DATABASE_URL não parece uma URL de postgres: {POSTGRES_DE_TESTE!r}\n"
            "Cole a connection string do Neon ou do Supabase, no formato:\n"
            "  postgresql://usuario:senha@host.neon.tech/banco?sslmode=require"
        )

    from sqlalchemy import inspect, select, text
    from sqlalchemy.orm import sessionmaker

    from app.db import criar_engine, normalizar_url
    from app.garimpo import registrar_feedback
    from app.models import Artist, Discovery, SeedArtist, User, Veredito

    url = normalizar_url(POSTGRES_DE_TESTE)
    engine = criar_engine(url)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))

    ambiente = {**os.environ, "DATABASE_URL": url, "LASTFM_API_KEY": "chave-de-teste"}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=RAIZ, env=ambiente, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr

    # o schema saiu igual ao dos models?
    tabelas = set(inspect(engine).get_table_names()) - {"alembic_version"}
    from app.models import Base

    assert tabelas == set(Base.metadata.tables)

    # e o caminho completo funciona?
    Fabrica = sessionmaker(bind=engine, expire_on_commit=False)
    with Fabrica() as s:
        user = User(email="pg@teste.com", password_hash="x")
        s.add(user)
        s.flush()
        s.add(SeedArtist(user_id=user.id, nome="Duster", ordem=0))
        alvo = Artist(nome="Alvo", nome_norm="alvo", listeners=5000)
        ponte = Artist(nome="Ponte", nome_norm="ponte", listeners=90000)
        alvo.tags = ["slowcore"]
        s.add_all([alvo, ponte])
        s.flush()

        d = Discovery(
            user_id=user.id, artist_id=alvo.id, ponte_id=ponte.id,
            score=0.3, afinidade=0.8, raridade=0.4, coerencia=0.6,
            n_rotas=1, semana="2026-W36",
        )
        d.rota = [{"semente": "Duster", "ponte": "Ponte", "candidato": "Alvo",
                   "peso": 0.5, "passos": []}]
        s.add(d)
        s.flush()

        registrar_feedback(s, d, Veredito.APROVADO)
        s.commit()

        # o json guardado como texto volta inteiro?
        lida = s.scalar(select(Discovery))
        assert lida.rota[0]["ponte"] == "Ponte"
        assert lida.artista.tags == ["slowcore"]
        assert lida.feedback.veredito == Veredito.APROVADO

        # a data voltou naive, como no sqlite?
        assert lida.criado_em.tzinfo is None
