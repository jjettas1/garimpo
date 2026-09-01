"""Todas as tabelas do projeto, num arquivo só.

São nove tabelas pequenas e muito ligadas entre si; separar em vários
arquivos aqui só criaria import circular sem ganho nenhum.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def agora() -> datetime:
    """Sempre UTC, e sempre **naive** (sem tzinfo).

    Parece errado, mas é o único jeito consistente com SQLite: ele guarda
    datetime como texto e descarta o fuso em silêncio na escrita. Se a gente
    devolvesse datetime ciente do fuso, toda comparação entre um valor lido do
    banco (naive) e um valor novo (ciente) estouraria com TypeError.

    Então a regra do projeto é: **no banco e no código, tudo é UTC naive**.
    Fuso horário só existe na hora de mostrar pro usuário.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalizar_nome(nome: str) -> str:
    """Chave de comparação de artista: sem acento, sem caixa, sem espaço sobrando.

    É o que impede 'Bjork', 'björk' e 'BJÖRK' de virarem três linhas.
    """
    sem_acento = unicodedata.normalize("NFKD", nome)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- enums


class TipoCodigo(StrEnum):
    CADASTRO = "cadastro"
    RECUPERACAO = "recuperacao"


class Veredito(StrEnum):
    APROVADO = "aprovado"
    PULADO = "pulado"
    BANIDO = "banido"


class StatusRun(StrEnum):
    RODANDO = "rodando"
    PRONTO = "pronto"
    ERRO = "erro"


# ---------------------------------------------------------------- contas


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email_verificado: Mapped[bool] = mapped_column(default=False, nullable=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, nullable=False)

    # faixa de ouvintes que o usuário aceita ver. não estava no schema da spec,
    # mas a tela /sementes precisa guardar isso em algum lugar.
    piso_ouvintes: Mapped[int] = mapped_column(Integer, default=1_000, nullable=False)
    teto_ouvintes: Mapped[int] = mapped_column(Integer, default=200_000, nullable=False)

    sementes: Mapped[list[SeedArtist]] = relationship(
        back_populates="user", cascade="all, delete-orphan", order_by="SeedArtist.ordem"
    )
    descobertas: Mapped[list[Discovery]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class VerificationCode(Base):
    __tablename__ = "verification_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    codigo_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo: Mapped[str] = mapped_column(String(20), default=TipoCodigo.CADASTRO, nullable=False)
    expira_em: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    usado_em: Mapped[datetime | None] = mapped_column(DateTime)
    tentativas: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, nullable=False)

    __table_args__ = (Index("ix_codes_user_tipo", "user_id", "tipo"),)


class Session_(Base):
    """Sessão de login. O nome da classe leva underscore pra não colidir com
    a Session do SQLAlchemy; a tabela continua sendo `sessions`."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    expira_em: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255))


class EnvioCodigo(Base):
    """Registro de cada envio de código, só pra contar o rate limit.

    Não estava na spec como tabela, mas o limite de 3 por email/hora e 10 por
    IP/hora precisa de histórico em algum lugar — e a spec pede sem Redis.
    """

    __tablename__ = "envios_codigo"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, nullable=False)

    __table_args__ = (
        Index("ix_envios_email_data", "email", "criado_em"),
        Index("ix_envios_ip_data", "ip", "criado_em"),
    )


# ---------------------------------------------------------------- música


class SeedArtist(Base):
    __tablename__ = "seed_artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    nome: Mapped[str] = mapped_column(String(255), nullable=False)
    mbid: Mapped[str | None] = mapped_column(String(64))
    ordem: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped[User] = relationship(back_populates="sementes")

    __table_args__ = (UniqueConstraint("user_id", "nome", name="uq_semente_por_usuario"),)


class Artist(Base):
    """Cache do Last.fm. `atualizado_em` é o que decide se bate na API de novo."""

    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(255), nullable=False)
    nome_norm: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    mbid: Mapped[str | None] = mapped_column(String(64))
    listeners: Mapped[int | None] = mapped_column(Integer)
    playcount: Mapped[int | None] = mapped_column(Integer)
    tags_json: Mapped[str | None] = mapped_column(Text)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, nullable=False)

    @property
    def tags(self) -> list[str]:
        return json.loads(self.tags_json) if self.tags_json else []

    @tags.setter
    def tags(self, valores: list[str]) -> None:
        self.tags_json = json.dumps(valores, ensure_ascii=False)


class Discovery(Base):
    """Um artista descoberto, com a rota que levou até ele preservada."""

    __tablename__ = "discoveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"), nullable=False)
    rota_json: Mapped[str] = mapped_column(Text, nullable=False)
    ponte_id: Mapped[int | None] = mapped_column(ForeignKey("artists.id"))
    score: Mapped[float] = mapped_column(Float, nullable=False)
    afinidade: Mapped[float] = mapped_column(Float, nullable=False)
    raridade: Mapped[float] = mapped_column(Float, nullable=False)
    coerencia: Mapped[float] = mapped_column(Float, nullable=False)
    n_rotas: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    semana: Mapped[str] = mapped_column(String(8), nullable=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, nullable=False)

    user: Mapped[User] = relationship(back_populates="descobertas")
    artista: Mapped[Artist] = relationship(foreign_keys=[artist_id])
    ponte: Mapped[Artist | None] = relationship(foreign_keys=[ponte_id])
    feedback: Mapped[Feedback | None] = relationship(
        back_populates="descoberta", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "artist_id", name="uq_descoberta_por_usuario"),
        Index("ix_descobertas_semana", "user_id", "semana"),
    )

    @property
    def rota(self) -> list[dict]:
        return json.loads(self.rota_json)

    @rota.setter
    def rota(self, passos: list[dict]) -> None:
        self.rota_json = json.dumps(passos, ensure_ascii=False)


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    discovery_id: Mapped[int] = mapped_column(
        ForeignKey("discoveries.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    veredito: Mapped[str] = mapped_column(String(16), nullable=False)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, nullable=False)

    descoberta: Mapped[Discovery] = relationship(back_populates="feedback")

    __table_args__ = (
        CheckConstraint(
            "veredito IN ('aprovado', 'pulado', 'banido')", name="ck_veredito_valido"
        ),
    )


class BridgeStat(Base):
    """Placar de cada ponte, por usuário. Derivado do feedback, mas
    materializado pra não recalcular a cada garimpo."""

    __tablename__ = "bridge_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"), nullable=False)
    aprovados: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "artist_id", name="uq_ponte_por_usuario"),)


class Run(Base):
    """Uma rodada de garimpo. É o que a tela fica pollando via HTMX."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=StatusRun.RODANDO, nullable=False)
    iniciado_em: Mapped[datetime] = mapped_column(DateTime, default=agora, nullable=False)
    terminado_em: Mapped[datetime | None] = mapped_column(DateTime)
    erro: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('rodando', 'pronto', 'erro')", name="ck_status_valido"),
        Index("ix_runs_user_status", "user_id", "status"),
    )
