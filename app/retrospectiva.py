"""Retrospectiva semanal: o que rendeu, e quais pontes renderam.

Só leitura e contas. Nada aqui grava nada.

As contas são feitas em Python, não em SQL de agregação, de propósito: são
dezenas de linhas por usuário, e o código fica legível. Se um dia isso virar
lento, o caminho é `GROUP BY` no banco — não é hoje.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.garimpo import peso_da_ponte
from app.models import Artist, Discovery, Feedback, User, Veredito


@dataclass(frozen=True)
class PonteDaSemana:
    """Quanto uma ponte rendeu numa semana específica."""

    nome: str
    aprovados: int
    total: int

    @property
    def taxa(self) -> float:
        return self.aprovados / self.total if self.total else 0.0


@dataclass(frozen=True)
class ResumoSemana:
    semana: str
    descobertas: int
    com_veredito: int
    aprovados: int
    media_ouvintes: int | None
    pontes: list[PonteDaSemana]

    @property
    def taxa(self) -> float | None:
        """None quando ninguém julgou nada na semana — não é 0%, é 'não sei'."""
        if not self.com_veredito:
            return None
        return self.aprovados / self.com_veredito

    @property
    def intervalo(self) -> tuple[date, date] | None:
        return intervalo_da_semana(self.semana)


@dataclass(frozen=True)
class PesoAtual:
    """O peso que a ponte tem *agora*, e que vale pra próxima rodada."""

    nome: str
    aprovados: int
    total: int
    peso: float

    @property
    def rende(self) -> bool:
        return self.peso > 1.0


def intervalo_da_semana(semana: str) -> tuple[date, date] | None:
    """`2026-W36` vira (segunda, domingo). None se o formato não bater."""
    try:
        ano, numero = semana.split("-W")
        segunda = datetime.fromisocalendar(int(ano), int(numero), 1).date()
        domingo = datetime.fromisocalendar(int(ano), int(numero), 7).date()
    except (ValueError, AttributeError):
        return None
    return segunda, domingo


def _linhas(sessao: Session, user: User) -> list[tuple[Discovery, Feedback | None, Artist, Artist | None]]:
    """Descobertas do usuário com o veredito, o artista e a ponte já juntos."""
    alvo = Artist.__table__.alias("alvo")
    ponte = Artist.__table__.alias("ponte")
    consulta = (
        select(Discovery, Feedback, alvo.c.nome, alvo.c.listeners, ponte.c.nome)
        .join(alvo, alvo.c.id == Discovery.artist_id)
        .outerjoin(ponte, ponte.c.id == Discovery.ponte_id)
        .outerjoin(Feedback, Feedback.discovery_id == Discovery.id)
        .where(Discovery.user_id == user.id)
    )
    return list(sessao.execute(consulta))


def resumo_por_semana(sessao: Session, user: User) -> list[ResumoSemana]:
    """Uma linha por semana, da mais recente pra mais antiga."""
    por_semana: dict[str, list] = {}
    for linha in _linhas(sessao, user):
        por_semana.setdefault(linha[0].semana, []).append(linha)

    resumos: list[ResumoSemana] = []
    for semana in sorted(por_semana, reverse=True):
        linhas = por_semana[semana]
        julgadas = [x for x in linhas if x[1] is not None]
        aprovadas = [x for x in julgadas if x[1].veredito == Veredito.APROVADO]

        ouvintes = [x[3] for x in aprovadas if x[3]]
        media = round(sum(ouvintes) / len(ouvintes)) if ouvintes else None

        # ranking das pontes daquela semana
        placar: dict[str, list[int]] = {}
        for descoberta, feedback, _, _, nome_ponte in julgadas:
            if not nome_ponte:
                continue
            conta = placar.setdefault(nome_ponte, [0, 0])
            conta[1] += 1
            if feedback.veredito == Veredito.APROVADO:
                conta[0] += 1

        pontes = [PonteDaSemana(nome, a, t) for nome, (a, t) in placar.items()]
        pontes.sort(key=lambda p: (p.aprovados, p.taxa), reverse=True)

        resumos.append(
            ResumoSemana(
                semana=semana,
                descobertas=len(linhas),
                com_veredito=len(julgadas),
                aprovados=len(aprovadas),
                media_ouvintes=media,
                pontes=pontes,
            )
        )
    return resumos


def pesos_das_pontes(sessao: Session, user: User, minimo: int = 1) -> list[PesoAtual]:
    """O loop fechado, em números: quanto cada ponte pesa na próxima rodada.

    É o mesmo `peso_da_ponte` que o motor usa pra pontuar — aqui só está
    sendo mostrado, não recalculado de outro jeito.
    """
    from app.models import BridgeStat

    consulta = (
        select(BridgeStat, Artist.nome)
        .join(Artist, Artist.id == BridgeStat.artist_id)
        .where(BridgeStat.user_id == user.id, BridgeStat.total >= minimo)
    )
    pesos = [
        PesoAtual(
            nome=nome,
            aprovados=placar.aprovados,
            total=placar.total,
            peso=peso_da_ponte(placar.aprovados, placar.total),
        )
        for placar, nome in sessao.execute(consulta)
    ]
    pesos.sort(key=lambda p: (p.peso, p.total), reverse=True)
    return pesos
