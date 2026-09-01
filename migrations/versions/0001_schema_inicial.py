"""schema inicial

Revision ID: 0001
Revises:
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("email_verificado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.Column("piso_ouvintes", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("teto_ouvintes", sa.Integer(), nullable=False, server_default="200000"),
    )

    op.create_table(
        "verification_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("codigo_hash", sa.String(255), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False, server_default="cadastro"),
        sa.Column("expira_em", sa.DateTime(), nullable=False),
        sa.Column("usado_em", sa.DateTime()),
        sa.Column("tentativas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_codes_user_tipo", "verification_codes", ["user_id", "tipo"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False, unique=True),
        sa.Column("expira_em", sa.DateTime(), nullable=False),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.Column("user_agent", sa.String(255)),
    )

    op.create_table(
        "envios_codigo",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_envios_email_data", "envios_codigo", ["email", "criado_em"])
    op.create_index("ix_envios_ip_data", "envios_codigo", ["ip", "criado_em"])

    op.create_table(
        "artists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nome", sa.String(255), nullable=False),
        sa.Column("nome_norm", sa.String(255), nullable=False, unique=True),
        sa.Column("mbid", sa.String(64)),
        sa.Column("listeners", sa.Integer()),
        sa.Column("playcount", sa.Integer()),
        sa.Column("tags_json", sa.Text()),
        sa.Column("atualizado_em", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "seed_artists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("nome", sa.String(255), nullable=False),
        sa.Column("mbid", sa.String(64)),
        sa.Column("ordem", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "nome", name="uq_semente_por_usuario"),
    )

    op.create_table(
        "discoveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artist_id", sa.Integer(), sa.ForeignKey("artists.id"), nullable=False),
        sa.Column("rota_json", sa.Text(), nullable=False),
        sa.Column("ponte_id", sa.Integer(), sa.ForeignKey("artists.id")),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("afinidade", sa.Float(), nullable=False),
        sa.Column("raridade", sa.Float(), nullable=False),
        sa.Column("coerencia", sa.Float(), nullable=False),
        sa.Column("n_rotas", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("semana", sa.String(8), nullable=False),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "artist_id", name="uq_descoberta_por_usuario"),
    )
    op.create_index("ix_descobertas_semana", "discoveries", ["user_id", "semana"])

    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "discovery_id",
            sa.Integer(),
            sa.ForeignKey("discoveries.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("veredito", sa.String(16), nullable=False),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "veredito IN ('aprovado', 'pulado', 'banido')", name="ck_veredito_valido"
        ),
    )

    op.create_table(
        "bridge_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artist_id", sa.Integer(), sa.ForeignKey("artists.id"), nullable=False),
        sa.Column("aprovados", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "artist_id", name="uq_ponte_por_usuario"),
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="rodando"),
        sa.Column("iniciado_em", sa.DateTime(), nullable=False),
        sa.Column("terminado_em", sa.DateTime()),
        sa.Column("erro", sa.Text()),
        sa.CheckConstraint("status IN ('rodando', 'pronto', 'erro')", name="ck_status_valido"),
    )
    op.create_index("ix_runs_user_status", "runs", ["user_id", "status"])


def downgrade() -> None:
    for tabela in (
        "runs",
        "bridge_stats",
        "feedback",
        "discoveries",
        "seed_artists",
        "artists",
        "envios_codigo",
        "sessions",
        "verification_codes",
        "users",
    ):
        op.drop_table(tabela)
