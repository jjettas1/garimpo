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
