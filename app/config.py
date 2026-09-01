"""Configuração do projeto. Tudo vem de variável de ambiente."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def carregar_dotenv(caminho: Path | None = None) -> None:
    """Joga o conteúdo do .env dentro do os.environ.

    Não sobrescreve variável que já existe: quem exporta na mão manda mais
    que o arquivo.
    """
    caminho = caminho or RAIZ / ".env"
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        os.environ.setdefault(chave.strip(), valor.strip().strip("\"'"))


class ConfigFaltando(RuntimeError):
    """Variável de ambiente obrigatória que não foi preenchida."""


def _texto(chave: str, padrao: str = "", *, obrigatorio: bool = False) -> str:
    valor = os.environ.get(chave, "").strip() or padrao
    if obrigatorio and not valor:
        raise ConfigFaltando(f"falta a variável de ambiente {chave} (veja .env.example)")
    return valor


def _inteiro(chave: str, padrao: int) -> int:
    bruto = os.environ.get(chave, "").strip()
    return int(bruto) if bruto else padrao


def _decimal(chave: str, padrao: float) -> float:
    bruto = os.environ.get(chave, "").strip()
    return float(bruto) if bruto else padrao


@dataclass(frozen=True)
class Config:
    """Só dados. Nenhuma lógica aqui dentro."""

    ambiente: str
    database_url: str

    lastfm_api_key: str
    lastfm_req_por_segundo: float
    cache_artista_dias: int

    # argon2id. os padrões seguem a RFC 9106; os testes baixam pra não
    # gastar 64 MiB de RAM por hash.
    argon2_tempo: int
    argon2_memoria_kib: int
    argon2_paralelismo: int

    sessao_validade_dias: int

    fanout: int
    pontes_por_semente: int
    decaimento_salto: float
    piso_ouvintes: int
    teto_ouvintes: int
    expoente_raridade: float
    expoente_coerencia: float

    @property
    def producao(self) -> bool:
        return self.ambiente == "prod"

    @property
    def cookie_secure(self) -> bool:
        """Cookie de sessão só vai com Secure quando tem HTTPS na frente."""
        return self.producao


@lru_cache(maxsize=1)
def obter_config() -> Config:
    """Lê o ambiente uma vez só e guarda o resultado."""
    carregar_dotenv()
    ambiente = _texto("AMBIENTE", "dev")
    return Config(
        ambiente=ambiente,
        database_url=_texto("DATABASE_URL", f"sqlite:///{RAIZ / 'garimpo.db'}"),
        lastfm_api_key=_texto("LASTFM_API_KEY", obrigatorio=True),
        lastfm_req_por_segundo=_decimal("LASTFM_REQ_POR_SEGUNDO", 4.0),
        cache_artista_dias=_inteiro("CACHE_ARTISTA_DIAS", 14),
        argon2_tempo=_inteiro("ARGON2_TEMPO", 3),
        argon2_memoria_kib=_inteiro("ARGON2_MEMORIA_KIB", 65_536),
        argon2_paralelismo=_inteiro("ARGON2_PARALELISMO", 4),
        sessao_validade_dias=_inteiro("SESSAO_VALIDADE_DIAS", 30),
        fanout=_inteiro("FANOUT", 30),
        pontes_por_semente=_inteiro("PONTES_POR_SEMENTE", 6),
        decaimento_salto=_decimal("DECAIMENTO_SALTO", 0.85),
        piso_ouvintes=_inteiro("PISO_OUVINTES", 1_000),
        teto_ouvintes=_inteiro("TETO_OUVINTES", 200_000),
        expoente_raridade=_decimal("EXPOENTE_RARIDADE", 0.7),
        expoente_coerencia=_decimal("EXPOENTE_COERENCIA", 0.5),
    )
