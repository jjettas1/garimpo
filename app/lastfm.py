"""Acesso ao Last.fm: cliente HTTP com rate limit + cache no banco.

A chave da API só existe aqui dentro. Nunca sai pro template nem pro JS.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config, obter_config
from app.models import Artist, agora, normalizar_nome

URL_BASE = "https://ws.audioscrobbler.com/2.0/"


class ErroLastfm(RuntimeError):
    """A API respondeu, mas com erro."""


@dataclass(frozen=True)
class Similar:
    """Um vizinho no grafo de similaridade."""

    nome: str
    match: float
    mbid: str | None = None


@dataclass(frozen=True)
class InfoArtista:
    nome: str
    mbid: str | None
    listeners: int
    playcount: int
    tags: list[str]


_TRAVA = threading.Lock()
_ULTIMA_CHAMADA = 0.0
"""Relógio compartilhado pelo processo inteiro.

Estas duas variáveis são de módulo, e não de instância, de propósito. O teto
de 5 req/s é do **Last.fm**, não de cada objeto: se cada cliente tivesse o
próprio contador, um autocomplete rodando junto com um garimpo daria dois
orçamentos de 4/s e estouraria o limite deles.

Vale por processo. Com mais de um worker do uvicorn cada um teria o seu, e aí
o teto precisaria sair daqui pra algum lugar compartilhado.
"""


def zerar_limitador() -> None:
    """Só pros testes: esquece a última chamada."""
    global _ULTIMA_CHAMADA
    with _TRAVA:
        _ULTIMA_CHAMADA = 0.0


class Limitador:
    """Segura as chamadas pra não passar do teto de requisições por segundo.

    O Last.fm aceita 5 req/s; a config manda 4 pra sobrar folga.
    """

    def __init__(self, por_segundo: float) -> None:
        self.intervalo = 1.0 / por_segundo if por_segundo > 0 else 0.0

    def esperar(self) -> None:
        """Segura a vez de quem chamou até dar o intervalo.

        A trava fica presa durante o sono: é ela que forma a fila. Cada
        chamada espera no máximo um intervalo além da que estiver na frente.
        """
        global _ULTIMA_CHAMADA
        if not self.intervalo:
            return
        with _TRAVA:
            espera = _ULTIMA_CHAMADA + self.intervalo - time.monotonic()
            if espera > 0:
                time.sleep(espera)
            _ULTIMA_CHAMADA = time.monotonic()


class ClienteLastfm:
    """Cliente cru da API. Não conhece o banco."""

    def __init__(self, config: Config | None = None, http: httpx.Client | None = None) -> None:
        self.config = config or obter_config()
        self.limitador = Limitador(self.config.lastfm_req_por_segundo)
        self._http = http or httpx.Client(timeout=15.0)

    def _chamar(self, metodo: str, **parametros) -> dict:
        self.limitador.esperar()
        resposta = self._http.get(
            URL_BASE,
            params={
                "method": metodo,
                "api_key": self.config.lastfm_api_key,
                "format": "json",
                **parametros,
            },
        )
        resposta.raise_for_status()
        dados = resposta.json()
        if "error" in dados:
            raise ErroLastfm(f"{metodo}: {dados.get('message', dados['error'])}")
        return dados

    def similares(self, nome: str, limite: int | None = None) -> list[Similar]:
        """artist.getSimilar — os vizinhos do artista, já ordenados por match."""
        limite = limite or self.config.fanout
        dados = self._chamar("artist.getSimilar", artist=nome, limit=limite, autocorrect=1)
        brutos = dados.get("similarartists", {}).get("artist", [])
        vizinhos = []
        for item in brutos:
            try:
                match = float(item.get("match") or 0.0)
            except (TypeError, ValueError):
                match = 0.0
            if match <= 0:
                continue
            vizinhos.append(Similar(nome=item["name"], match=match, mbid=item.get("mbid") or None))
        return vizinhos

    def info(self, nome: str) -> InfoArtista | None:
        """artist.getInfo — ouvintes, execuções e tags."""
        try:
            dados = self._chamar("artist.getInfo", artist=nome, autocorrect=1)
        except ErroLastfm:
            return None
        artista = dados.get("artist")
        if not artista:
            return None
        estatisticas = artista.get("stats", {})
        tags = [t["name"] for t in artista.get("tags", {}).get("tag", []) if t.get("name")]
        return InfoArtista(
            nome=artista.get("name", nome),
            mbid=artista.get("mbid") or None,
            listeners=int(estatisticas.get("listeners") or 0),
            playcount=int(estatisticas.get("playcount") or 0),
            tags=tags,
        )

    def buscar(self, termo: str, limite: int = 8) -> list[Similar]:
        """artist.search — usado pelo autocomplete da tela de sementes."""
        dados = self._chamar("artist.search", artist=termo, limit=limite)
        achados = dados.get("results", {}).get("artistmatches", {}).get("artist", [])
        return [Similar(nome=a["name"], match=0.0, mbid=a.get("mbid") or None) for a in achados]

    def fechar(self) -> None:
        self._http.close()


class CacheArtistas:
    """Camada entre o motor e a API: só bate no Last.fm quando o cache venceu."""

    def __init__(self, sessao: Session, cliente: ClienteLastfm, dias_de_validade: int | None = None):
        self.sessao = sessao
        self.cliente = cliente
        self.validade = timedelta(days=dias_de_validade or cliente.config.cache_artista_dias)

    def obter(self, nome: str) -> Artist | None:
        """Devolve o artista do banco, atualizando pelo Last.fm se estiver velho."""
        norm = normalizar_nome(nome)
        artista = self.sessao.scalar(select(Artist).where(Artist.nome_norm == norm))

        if artista is not None and not self._venceu(artista):
            return artista

        info = self.cliente.info(nome)
        if info is None:
            return artista  # sem resposta da API: fica com o que já tinha (ou nada)

        if artista is None:
            artista = Artist(nome=info.nome, nome_norm=norm)
            self.sessao.add(artista)

        artista.mbid = info.mbid or artista.mbid
        artista.listeners = info.listeners
        artista.playcount = info.playcount
        artista.tags = info.tags
        artista.atualizado_em = agora()
        self.sessao.flush()
        return artista

    def _venceu(self, artista: Artist) -> bool:
        return agora() - artista.atualizado_em > self.validade
