"""Envio de email, atrás de uma interface.

Em desenvolvimento o código de verificação só aparece no terminal — ninguém
precisa de caixa de entrada de verdade pra testar cadastro.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from app.config import Config


class EmailSender(Protocol):
    """O contrato. Quem envia email no projeto implementa isso."""

    def enviar(self, destino: str, assunto: str, corpo: str) -> None: ...


class ConsoleEmailSender:
    """Implementação de desenvolvimento: printa e pronto."""

    def enviar(self, destino: str, assunto: str, corpo: str) -> None:
        print("\n" + "=" * 60)
        print(f"[email] para: {destino}")
        print(f"[email] assunto: {assunto}")
        print("-" * 60)
        print(corpo)
        print("=" * 60 + "\n", flush=True)


class ResendEmailSender:
    """Implementação de produção, via API do Resend."""

    URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str, remetente: str) -> None:
        self.api_key = api_key
        self.remetente = remetente

    def enviar(self, destino: str, assunto: str, corpo: str) -> None:
        resposta = httpx.post(
            self.URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "from": self.remetente,
                "to": [destino],
                "subject": assunto,
                "text": corpo,
            },
            timeout=10.0,
        )
        resposta.raise_for_status()


def criar_correio(config: Config) -> EmailSender:
    """Escolhe a implementação pelo ambiente.

    Sem chave do Resend cai no console — de propósito: é melhor o cadastro
    funcionar em dev do que quebrar por falta de configuração.
    """
    if config.producao and config.resend_api_key:
        return ResendEmailSender(config.resend_api_key, config.email_remetente)
    return ConsoleEmailSender()


def texto_do_codigo(codigo: str, validade_minutos: int) -> tuple[str, str]:
    """Assunto e corpo do email de verificação. Texto puro, sem HTML."""
    assunto = f"{codigo} é o seu código do Garimpo"
    corpo = (
        f"Seu código de verificação é:\n\n"
        f"    {codigo}\n\n"
        f"Ele vale por {validade_minutos} minutos.\n\n"
        f"Se não foi você que pediu, pode ignorar este email — "
        f"ninguém entra na sua conta só com ele."
    )
    return assunto, corpo
