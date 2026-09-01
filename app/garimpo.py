"""Motor de descoberta.

A parte de cima do arquivo é matemática pura — sem banco, sem rede, sem
relógio. É ela que os testes cobrem. A parte de baixo só orquestra: busca no
Last.fm, aplica as funções puras e grava.

O caminho é sempre:

    semente ──match₁──> ponte ──match₂──> candidato

e o que o produto vende é justamente esse caminho, não o nome no fim dele.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config, obter_config
from app.lastfm import CacheArtistas, ClienteLastfm
from app.models import (
    Artist,
    BridgeStat,
    Discovery,
    Feedback,
    Run,
    SeedArtist,
    StatusRun,
    User,
    Veredito,
    agora,
    normalizar_nome,
)

PISO_COERENCIA = 0.05
"""Artista sem tag nenhuma não é zerado — só fica com coerência baixa."""

TETO_PESO_ROTA = 0.999
"""O peso de uma rota nunca chega a 1, senão a afinidade satura em 1 e para
de distinguir um candidato do outro."""


# ============================================================ matemática pura


def peso_da_ponte(aprovados: int, total: int) -> float:
    """Quanto a ponte vale, pelo histórico de feedback do usuário.

    Suavização de Laplace: `(aprovados + 1) / (total + 2)`. Sem histórico dá
    0.5, e a gente normaliza pra 1.0 — ponte nova entra neutra, nem premiada
    nem punida. Ponte que só acerta tende a 2.0, ponte que só erra tende a 0.
    """
    return 2.0 * (aprovados + 1) / (total + 2)


def peso_da_rota(matches: list[float], peso_ponte: float, decaimento: float) -> float:
    """Peso de um caminho: produto dos matches × decaimento por salto × ponte.

    O decaimento é o que faz uma descoberta a dois saltos valer menos que a
    mesma similaridade a um salto só.
    """
    if not matches:
        return 0.0
    produto = math.prod(matches)
    saltos = len(matches)
    peso = produto * (decaimento ** (saltos - 1)) * peso_ponte
    return min(max(peso, 0.0), TETO_PESO_ROTA)


def combinar_afinidade(pesos: list[float]) -> float:
    """`afinidade = 1 - Π(1 - wᵢ)`.

    Chegar no mesmo artista por três caminhos fracos vale mais que por um só,
    mas nunca vira certeza — a conta é a probabilidade de pelo menos um
    caminho "acertar".
    """
    complemento = 1.0
    for peso in pesos:
        complemento *= 1.0 - min(max(peso, 0.0), TETO_PESO_ROTA)
    # o mesmo teto das rotas vale aqui: com rota demais o float satura em 1.0
    # e aí todo candidato empata, que é justamente o que a gente não quer.
    return min(1.0 - complemento, TETO_PESO_ROTA)


def raridade(ouvintes: int, piso: int, teto: int) -> float:
    """1.0 no piso de ouvintes, 0.0 no teto, logarítmico no meio.

    Log porque a diferença entre 2 mil e 20 mil ouvintes importa muito mais
    que entre 180 mil e 198 mil.
    """
    piso = max(piso, 1)
    teto = max(teto, piso + 1)
    ouvintes = min(max(ouvintes, piso), teto)
    return 1.0 - (math.log(ouvintes) - math.log(piso)) / (math.log(teto) - math.log(piso))


def coerencia(tags_artista: list[str], tags_sementes: set[str]) -> float:
    """O quanto as tags do candidato encostam nas tags das sementes.

    Coeficiente de sobreposição (interseção sobre o menor dos dois conjuntos),
    não Jaccard: o universo de tags das sementes é grande e o Jaccard puxaria
    todo mundo pra perto de zero.
    """
    do_artista = {normalizar_nome(t) for t in tags_artista if t}
    das_sementes = {normalizar_nome(t) for t in tags_sementes if t}
    if not do_artista or not das_sementes:
        return PISO_COERENCIA
    comuns = len(do_artista & das_sementes)
    menor = min(len(do_artista), len(das_sementes))
    return max(comuns / menor, PISO_COERENCIA)


def calcular_score(
    afinidade: float,
    raridade_: float,
    coerencia_: float,
    expoente_raridade: float = 0.7,
    expoente_coerencia: float = 0.5,
) -> float:
    """`score = afinidade × raridade^0.7 × coerência^0.5`.

    Os expoentes menores que 1 achatam raridade e coerência: elas ajustam o
    ranking, mas quem manda é a afinidade.
    """
    return (
        afinidade
        * (max(raridade_, 0.0) ** expoente_raridade)
        * (max(coerencia_, 0.0) ** expoente_coerencia)
    )


def semana_de(momento: datetime | None = None) -> str:
    """Formato `2026-W34`, usado pra agrupar a retrospectiva."""
    momento = momento or agora()
    ano, semana, _ = momento.isocalendar()
    return f"{ano}-W{semana:02d}"


# ============================================================ estruturas


@dataclass(frozen=True)
class Passo:
    """Um salto da rota. Vira JSON e é o que a tela desenha com as setas."""

    de: str
    para: str
    match: float
    papel: str  # "semente->ponte" ou "ponte->candidato"

    def como_dict(self) -> dict:
        return {"de": self.de, "para": self.para, "match": round(self.match, 4), "papel": self.papel}


@dataclass
class Rota:
    semente: str
    ponte: str
    candidato: str
    passos: list[Passo]
    peso: float

    def como_dict(self) -> dict:
        return {
            "semente": self.semente,
            "ponte": self.ponte,
            "candidato": self.candidato,
            "peso": round(self.peso, 4),
            "passos": [p.como_dict() for p in self.passos],
        }


@dataclass
class Candidato:
    """Um artista alcançado, com todas as rotas que chegaram nele."""

    nome: str
    rotas: list[Rota] = field(default_factory=list)

    @property
    def afinidade(self) -> float:
        return combinar_afinidade([r.peso for r in self.rotas])

    @property
    def melhor_rota(self) -> Rota:
        return max(self.rotas, key=lambda r: r.peso)


@dataclass
class Descoberta:
    """Resultado pronto pra virar linha na tabela `discoveries`."""

    artista: Artist
    ponte: Artist | None
    rotas: list[Rota]
    afinidade: float
    raridade: float
    coerencia: float
    score: float


# ============================================================ orquestração


def _peso_da_ponte_do_usuario(sessao: Session, user_id: int, ponte: Artist | None) -> float:
    """Busca o placar da ponte pra esse usuário. Sem placar, ponte neutra."""
    if ponte is None:
        return 1.0
    placar = sessao.scalar(
        select(BridgeStat).where(BridgeStat.user_id == user_id, BridgeStat.artist_id == ponte.id)
    )
    if placar is None:
        return 1.0
    return peso_da_ponte(placar.aprovados, placar.total)


def _nomes_bloqueados(sessao: Session, user: User) -> set[str]:
    """Quem não pode aparecer de novo: as próprias sementes, o que já foi
    aprovado e o que foi banido."""
    bloqueados = {normalizar_nome(s.nome) for s in user.sementes}
    ja_vistos = sessao.execute(
        select(Artist.nome_norm)
        .select_from(Discovery)
        .join(Artist, Artist.id == Discovery.artist_id)
        .outerjoin(Feedback, Feedback.discovery_id == Discovery.id)
        .where(
            Discovery.user_id == user.id,
            Feedback.veredito.in_([Veredito.APROVADO, Veredito.BANIDO]),
        )
    ).scalars()
    bloqueados.update(ja_vistos)
    return bloqueados


def coletar_candidatos(
    sessao: Session,
    user: User,
    cliente: ClienteLastfm,
    cache: CacheArtistas,
    config: Config,
) -> dict[str, Candidato]:
    """Percorre o grafo: sementes → pontes → candidatos, guardando as rotas."""
    candidatos: dict[str, Candidato] = {}
    bloqueados = _nomes_bloqueados(sessao, user)

    for semente in user.sementes:
        vizinhos = cliente.similares(semente.nome, limite=config.fanout)
        pontes = vizinhos[: config.pontes_por_semente]

        for ponte in pontes:
            artista_ponte = cache.obter(ponte.nome)
            fator_ponte = _peso_da_ponte_do_usuario(sessao, user.id, artista_ponte)
            nomes_da_ponte = {normalizar_nome(ponte.nome)}

            for vizinho in cliente.similares(ponte.nome, limite=config.fanout):
                norm = normalizar_nome(vizinho.nome)
                if norm in bloqueados or norm in nomes_da_ponte:
                    continue

                peso = peso_da_rota(
                    [ponte.match, vizinho.match], fator_ponte, config.decaimento_salto
                )
                if peso <= 0:
                    continue

                rota = Rota(
                    semente=semente.nome,
                    ponte=ponte.nome,
                    candidato=vizinho.nome,
                    passos=[
                        Passo(semente.nome, ponte.nome, ponte.match, "semente->ponte"),
                        Passo(ponte.nome, vizinho.nome, vizinho.match, "ponte->candidato"),
                    ],
                    peso=peso,
                )
                candidatos.setdefault(norm, Candidato(nome=vizinho.nome)).rotas.append(rota)

    return candidatos


def pontuar_candidatos(
    sessao: Session,
    user: User,
    candidatos: dict[str, Candidato],
    cache: CacheArtistas,
    config: Config,
    tags_sementes: set[str],
) -> list[Descoberta]:
    """Busca ouvintes e tags de cada candidato, filtra pela faixa e pontua."""
    piso, teto = user.piso_ouvintes, user.teto_ouvintes
    descobertas: list[Descoberta] = []

    for candidato in candidatos.values():
        artista = cache.obter(candidato.nome)
        if artista is None or artista.listeners is None:
            continue
        if not (piso <= artista.listeners <= teto):
            continue  # famoso demais ou fantasma demais

        afinidade = candidato.afinidade
        rar = raridade(artista.listeners, piso, teto)
        coe = coerencia(artista.tags, tags_sementes)
        score = calcular_score(
            afinidade, rar, coe, config.expoente_raridade, config.expoente_coerencia
        )

        descobertas.append(
            Descoberta(
                artista=artista,
                ponte=cache.obter(candidato.melhor_rota.ponte),
                rotas=sorted(candidato.rotas, key=lambda r: r.peso, reverse=True),
                afinidade=afinidade,
                raridade=rar,
                coerencia=coe,
                score=score,
            )
        )

    descobertas.sort(key=lambda d: d.score, reverse=True)
    return descobertas


def tags_das_sementes(user: User, cache: CacheArtistas) -> set[str]:
    """União das tags de todas as sementes — o universo de gosto do usuário."""
    universo: set[str] = set()
    for semente in user.sementes:
        artista = cache.obter(semente.nome)
        if artista is not None:
            universo.update(artista.tags)
    return universo


def gravar_descobertas(
    sessao: Session, user: User, descobertas: list[Descoberta], limite: int
) -> list[Discovery]:
    """Grava as melhores. Artista já descoberto antes é atualizado, não duplicado."""
    semana = semana_de()
    gravadas: list[Discovery] = []

    for descoberta in descobertas[:limite]:
        linha = sessao.scalar(
            select(Discovery).where(
                Discovery.user_id == user.id, Discovery.artist_id == descoberta.artista.id
            )
        )
        if linha is None:
            linha = Discovery(user_id=user.id, artist_id=descoberta.artista.id, semana=semana)
            sessao.add(linha)

        linha.rota = [r.como_dict() for r in descoberta.rotas]
        linha.ponte_id = descoberta.ponte.id if descoberta.ponte else None
        linha.score = descoberta.score
        linha.afinidade = descoberta.afinidade
        linha.raridade = descoberta.raridade
        linha.coerencia = descoberta.coerencia
        linha.n_rotas = len(descoberta.rotas)
        linha.semana = semana
        gravadas.append(linha)

    sessao.flush()
    return gravadas


def _sem_a_chave(mensagem: str, config: Config) -> str:
    """Tira a chave do Last.fm de uma mensagem de erro.

    Sem isso a chave vazaria de um jeito bobo: o httpx põe a URL inteira na
    exceção, a URL tem `api_key=...`, a mensagem vai pra tabela `runs` e a
    tela de garimpo mostra ela pro usuário.
    """
    if config.lastfm_api_key:
        mensagem = mensagem.replace(config.lastfm_api_key, "***")
    return mensagem


def garimpar(
    sessao: Session,
    user: User,
    cliente: ClienteLastfm | None = None,
    config: Config | None = None,
    limite: int = 20,
    rodada_id: int | None = None,
) -> list[Discovery]:
    """Uma rodada inteira, do começo ao fim, registrada na tabela `runs`.

    Isso aqui **não** roda dentro do request HTTP: são centenas de chamadas ao
    Last.fm. Quem chama pela web é o BackgroundTasks do FastAPI.

    O `rodada_id` existe porque a web precisa abrir a linha de `runs` **dentro
    do request** — senão, entre a resposta e a tarefa de fundo começar, a tela
    não teria o que mostrar. Sem isso viriam duas linhas por rodada, e a
    primeira ficaria em `rodando` pra sempre, travando o polling.
    """
    config = config or obter_config()
    cliente = cliente or ClienteLastfm(config)
    cache = CacheArtistas(sessao, cliente)

    rodada = sessao.get(Run, rodada_id) if rodada_id is not None else None
    if rodada is None:
        rodada = Run(user_id=user.id, status=StatusRun.RODANDO)
        sessao.add(rodada)
    sessao.flush()

    try:
        universo = tags_das_sementes(user, cache)
        candidatos = coletar_candidatos(sessao, user, cliente, cache, config)
        descobertas = pontuar_candidatos(sessao, user, candidatos, cache, config, universo)
        gravadas = gravar_descobertas(sessao, user, descobertas, limite)
    except Exception as erro:
        rodada.status = StatusRun.ERRO
        rodada.erro = _sem_a_chave(f"{type(erro).__name__}: {erro}", config)
        rodada.terminado_em = agora()
        sessao.flush()
        raise

    rodada.status = StatusRun.PRONTO
    rodada.terminado_em = agora()
    sessao.flush()
    return gravadas


def registrar_feedback(sessao: Session, descoberta: Discovery, veredito: Veredito) -> None:
    """Guarda o veredito e atualiza o placar da ponte na mesma tacada.

    É esse placar que fecha o loop: ponte que rende passa a pesar mais na
    próxima rodada daquele usuário.

    Dar o mesmo veredito duas vezes não muda nada, e trocar de ideia move o
    placar em vez de somar de novo. Sem isso, um clique duplo (ou um refresh
    depois do POST) inflava o `total` da ponte e estragava o peso dela pra
    sempre — e `bridge_stats` é materializado, ninguém recalcula depois.
    """
    anterior = descoberta.feedback.veredito if descoberta.feedback is not None else None
    if anterior == veredito:
        return  # nada mudou

    if descoberta.feedback is None:
        descoberta.feedback = Feedback(veredito=veredito)
    else:
        descoberta.feedback.veredito = veredito

    if descoberta.ponte_id is None:
        sessao.flush()
        return

    placar = sessao.scalar(
        select(BridgeStat).where(
            BridgeStat.user_id == descoberta.user_id,
            BridgeStat.artist_id == descoberta.ponte_id,
        )
    )
    if placar is None:
        placar = BridgeStat(
            user_id=descoberta.user_id, artist_id=descoberta.ponte_id, aprovados=0, total=0
        )
        sessao.add(placar)

    if anterior is None:
        placar.total += 1                      # veredito novo: entra na conta
    elif anterior == Veredito.APROVADO:
        placar.aprovados = max(placar.aprovados - 1, 0)   # deixou de aprovar

    if veredito == Veredito.APROVADO:
        placar.aprovados += 1

    sessao.flush()
