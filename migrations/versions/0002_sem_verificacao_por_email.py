"""remove a verificacao por email

O cadastro passou a entrar direto: some o fluxo de código de 6 dígitos, a
tabela que contava os envios pro rate limit, e a coluna que dizia se o email
tinha sido confirmado.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("envios_codigo")       # os índices caem junto com a tabela
    op.drop_table("verification_codes")

    # o SQLite não sabe DROP COLUMN de verdade; o batch recria a tabela
    with op.batch_alter_table("users") as tabela:
        tabela.drop_column("email_verificado")


def downgrade() -> None:
    with op.batch_alter_table("users") as tabela:
        tabela.add_column(
            sa.Column("email_verificado", sa.Boolean(), nullable=False, server_default=sa.true())
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
        "envios_codigo",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_envios_email_data", "envios_codigo", ["email", "criado_em"])
    op.create_index("ix_envios_ip_data", "envios_codigo", ["ip", "criado_em"])
