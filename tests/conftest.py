"""Fixtures dos testes.

Duas regras aqui: banco em memória (nada escrito em disco) e **nenhuma
chamada real ao Last.fm** — o cliente é falso e devolve um grafo fixo.
"""

from __future__ import annotations

import os

import pytest

# a config é lida na primeira chamada de obter_config(); precisa estar
# preenchida antes de qualquer import do app.
os.environ.setdefault("LASTFM_API_KEY", "chave-de-teste")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("AMBIENTE", "dev")

# argon2 de brinquedo: em produção são 64 MiB por hash, o que deixaria a
# suíte inteira lenta demais pra ser rodada a cada mudança.
os.environ.setdefault("ARGON2_TEMPO", "1")
os.environ.setdefault("ARGON2_MEMORIA_KIB", "8")
os.environ.setdefault("ARGON2_PARALELISMO", "1")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import obter_config  # noqa: E402
from app.lastfm import InfoArtista, Similar  # noqa: E402
from app.models import Base, SeedArtist, User  # noqa: E402

# grafo de mentira: semente -> pontes -> candidatos
GRAFO = {
    "Semente": [("Ponte A", 0.9), ("Ponte B", 0.8)],
    "Ponte A": [("Alvo", 0.7), ("Famoso", 0.9)],
    "Ponte B": [("Alvo", 0.6), ("Raro", 0.5)],
}

CATALOGO = {
    "Semente": (500_000, ["slowcore", "rock"]),
    "Semente Dois": (400_000, ["slowcore"]),   # sem vizinhos no grafo, só pra
    "Semente Tres": (300_000, ["rock"]),       # fechar o mínimo de 3 sementes
    "Ponte A": (300_000, ["slowcore", "indie"]),
    "Ponte B": (250_000, ["rock", "indie"]),
    "Alvo": (5_000, ["slowcore", "rock"]),      # dentro da faixa
    "Famoso": (5_000_000, ["rock"]),            # acima do teto
    "Raro": (800, ["noise"]),                   # abaixo do piso
}


class ClienteFalso:
    """Mesma interface do ClienteLastfm, sem rede. Conta as chamadas pra dar
    pra testar o cache."""

    def __init__(self, config):
        self.config = config
        self.chamadas_info: list[str] = []
        self.chamadas_similares: list[str] = []

    def fechar(self) -> None:
        pass

    def buscar(self, termo: str, limite: int = 8) -> list[Similar]:
        achados = [n for n in CATALOGO if termo.lower() in n.lower()]
        return [Similar(nome=n, match=0.0) for n in achados[:limite]]

    def similares(self, nome: str, limite: int | None = None) -> list[Similar]:
        self.chamadas_similares.append(nome)
        vizinhos = [Similar(nome=n, match=m) for n, m in GRAFO.get(nome, [])]
        return vizinhos[: limite or len(vizinhos)]

    def info(self, nome: str) -> InfoArtista | None:
        self.chamadas_info.append(nome)
        if nome not in CATALOGO:
            return None
        ouvintes, tags = CATALOGO[nome]
        return InfoArtista(
            nome=nome, mbid=None, listeners=ouvintes, playcount=ouvintes * 10, tags=tags
        )


@pytest.fixture
def config():
    return obter_config()


@pytest.fixture
def sessao():
    """Banco novinho em memória a cada teste.

    StaticPool porque SQLite em memória cria **um banco por conexão**: sem
    isso o TestClient, que roda o app em outra thread, abriria uma segunda
    conexão e encontraria um banco vazio.
    """
    engine = create_engine(
        "sqlite://",
        future=True,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    fabrica = sessionmaker(bind=engine, expire_on_commit=False)
    with fabrica() as s:
        yield s


@pytest.fixture
def cliente(config):
    return ClienteFalso(config)


@pytest.fixture
def usuario(sessao):
    user = User(
        email="teste@exemplo.com",
        password_hash="hash-de-teste",
        email_verificado=True,
        piso_ouvintes=1_000,
        teto_ouvintes=200_000,
    )
    sessao.add(user)
    sessao.flush()
    sessao.add(SeedArtist(user_id=user.id, nome="Semente", ordem=0))
    sessao.flush()
    sessao.refresh(user)
    return user


# ------------------------------------------------------------ auth


class CorreioFalso:
    """EmailSender que guarda tudo em vez de mandar."""

    def __init__(self) -> None:
        self.enviados: list[tuple[str, str, str]] = []

    def enviar(self, destino: str, assunto: str, corpo: str) -> None:
        self.enviados.append((destino, assunto, corpo))

    @property
    def ultimo_codigo(self) -> str:
        """Pesca os 6 dígitos do último email enviado."""
        import re

        assert self.enviados, "nenhum email foi enviado"
        achado = re.search(r"\b(\d{6})\b", self.enviados[-1][2])
        assert achado, f"nenhum código no corpo: {self.enviados[-1][2]!r}"
        return achado.group(1)

    @property
    def destinos(self) -> list[str]:
        return [e[0] for e in self.enviados]


@pytest.fixture
def correio():
    return CorreioFalso()


@pytest.fixture
def cadastrado(sessao, correio, config):
    """Um usuário com email já confirmado e senha conhecida."""
    from app import auth

    user = auth.cadastrar(sessao, "jaime@exemplo.com", "senha-boa-123", correio, "10.0.0.1", config)
    auth.conferir_codigo(sessao, user, correio.ultimo_codigo, config=config)
    correio.enviados.clear()
    return user


@pytest.fixture
def cliente_web(sessao, correio, config, cliente):
    """TestClient com o banco de teste e o correio falso no lugar dos de verdade."""
    from fastapi.testclient import TestClient

    from app.web import app, pegar_config, pegar_correio, pegar_sessao

    from sqlalchemy.orm import sessionmaker as _sessionmaker

    from app.web import pegar_cliente_lastfm, pegar_fabrica, pegar_fabrica_lastfm

    def _sessao():
        yield sessao
        sessao.commit()

    app.dependency_overrides[pegar_sessao] = _sessao
    app.dependency_overrides[pegar_correio] = lambda: correio
    app.dependency_overrides[pegar_config] = lambda: config
    app.dependency_overrides[pegar_cliente_lastfm] = lambda: cliente
    app.dependency_overrides[pegar_fabrica_lastfm] = lambda: (lambda: cliente)
    # a tarefa de fundo abre sessão própria: precisa cair no mesmo banco
    app.dependency_overrides[pegar_fabrica] = lambda: _sessionmaker(
        bind=sessao.get_bind(), expire_on_commit=False
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def logado(cliente_web, sessao, cadastrado, config):
    """Cliente web já autenticado, com uma sessão de verdade no banco."""
    from app import auth
    from app.web import COOKIE_SESSAO

    token = auth.abrir_sessao_de_login(sessao, cadastrado, "pytest", config)
    cliente_web.cookies.set(COOKIE_SESSAO, token)
    return cliente_web


@pytest.fixture
def com_sementes(logado, sessao, cadastrado):
    """Usuário logado e com as três sementes mínimas."""
    from app.models import SeedArtist

    for ordem, nome in enumerate(["Semente", "Semente Dois", "Semente Tres"]):
        sessao.add(SeedArtist(user_id=cadastrado.id, nome=nome, ordem=ordem))
    sessao.flush()
    sessao.refresh(cadastrado)
    return logado


@pytest.fixture
def fabricar_descoberta(sessao):
    """Monta uma descoberta pronta, sem passar pelo motor.

    Os testes de histórico e retrospectiva precisam controlar exatamente o
    que entra — semana, ouvintes, ponte — e o grafo falso só produz um
    resultado.
    """
    from app.models import Artist, Discovery, normalizar_nome

    def artista(nome: str, ouvintes: int, tags: list[str] | None = None) -> Artist:
        norm = normalizar_nome(nome)
        existente = sessao.scalar(select(Artist).where(Artist.nome_norm == norm))
        if existente is not None:
            return existente
        novo = Artist(nome=nome, nome_norm=norm, listeners=ouvintes)
        novo.tags = tags or ["slowcore"]
        sessao.add(novo)
        sessao.flush()
        return novo

    def fabricar(
        user_id: int,
        nome: str,
        ouvintes: int = 10_000,
        ponte: str = "Uma Ponte",
        tags: list[str] | None = None,
        semana: str = "2026-W36",
    ) -> Discovery:
        alvo, aponte = artista(nome, ouvintes, tags), artista(ponte, 90_000)
        descoberta = Discovery(
            user_id=user_id, artist_id=alvo.id, ponte_id=aponte.id,
            score=0.3, afinidade=0.8, raridade=0.4, coerencia=0.6,
            n_rotas=1, semana=semana,
        )
        descoberta.rota = [{
            "semente": "Codeine", "ponte": ponte, "candidato": nome, "peso": 0.5,
            "passos": [
                {"de": "Codeine", "para": ponte, "match": 0.8, "papel": "semente->ponte"},
                {"de": ponte, "para": nome, "match": 0.6, "papel": "ponte->candidato"},
            ],
        }]
        sessao.add(descoberta)
        sessao.flush()
        return descoberta

    return fabricar
