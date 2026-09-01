"""Autenticação: senha e sessão.

Feito na mão, sem biblioteca de auth pronta.

Não há verificação de email: quem cadastra entra na hora. A consequência
está registrada em `cadastrar()` — vale ler antes de mexer.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from functools import lru_cache

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config, obter_config
from app.models import Session_, User, agora

MENSAGEM_CREDENCIAL_INVALIDA = "email ou senha incorretos"
"""Uma única mensagem pros dois casos, byte a byte.

Se "email não cadastrado" e "senha incorreta" fossem textos diferentes,
qualquer pessoa poderia descobrir quem tem conta aqui só chutando emails.
"""

TAMANHO_MINIMO_SENHA = 8
TAMANHO_TOKEN_SESSAO = 32  # bytes


# ---------------------------------------------------------------- erros


class ErroDeAuth(Exception):
    """Base de tudo que pode dar errado aqui."""


class CredenciaisInvalidas(ErroDeAuth):
    def __init__(self) -> None:
        super().__init__(MENSAGEM_CREDENCIAL_INVALIDA)


class EmailJaCadastrado(ErroDeAuth):
    pass


class SenhaFraca(ErroDeAuth):
    pass


class EmailInvalido(ErroDeAuth):
    pass


# ---------------------------------------------------------------- senha


@lru_cache(maxsize=1)
def hasher(config: Config | None = None) -> PasswordHasher:
    """argon2id, com os parâmetros vindos da config.

    O PasswordHasher do argon2-cffi já usa Type.ID por padrão, mas deixo
    explícito: argon2**i** e argon2**d** não servem aqui.
    """
    config = config or obter_config()
    return PasswordHasher(
        time_cost=config.argon2_tempo,
        memory_cost=config.argon2_memoria_kib,
        parallelism=config.argon2_paralelismo,
        type=Type.ID,
    )


@lru_cache(maxsize=1)
def _hash_dummy() -> str:
    """Um hash real de uma senha que ninguém tem.

    Serve pra gastar exatamente o mesmo tempo quando o email não existe. Sem
    isso, "email inexistente" responderia na hora e "senha errada" demoraria
    os ~100ms do argon2 — e o cronômetro entregaria quem tem conta, mesmo com
    a mensagem sendo idêntica.
    """
    return hasher().hash("senha-que-nao-existe-em-lugar-nenhum")


def gerar_hash_de_senha(senha: str) -> str:
    if len(senha) < TAMANHO_MINIMO_SENHA:
        raise SenhaFraca(f"a senha precisa de pelo menos {TAMANHO_MINIMO_SENHA} caracteres")
    return hasher().hash(senha)


def conferir_senha(hash_guardado: str, senha: str) -> bool:
    """True se a senha bate. Nunca levanta exceção por hash estragado."""
    try:
        hasher().verify(hash_guardado, senha)
        return True
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError):
        # hash corrompido, ou conta criada pela CLI sem senha. gasta o mesmo
        # tempo de um confronto de verdade, senão o relógio entrega a conta.
        try:
            hasher().verify(_hash_dummy(), senha)
        except Exception:
            pass
        return False


def normalizar_email(email: str) -> str:
    """Email é caso-insensível na prática. Guardar sem normalizar deixaria
    'Eu@X.com' e 'eu@x.com' virarem duas contas."""
    limpo = email.strip().lower()
    if limpo.count("@") != 1 or limpo.startswith("@") or limpo.endswith("@"):
        raise EmailInvalido("email inválido")
    dominio = limpo.split("@")[1]
    if "." not in dominio or dominio.startswith(".") or dominio.endswith("."):
        raise EmailInvalido("email inválido")
    return limpo


# ---------------------------------------------------------------- cadastro


def cadastrar(sessao: Session, email: str, senha: str, config: Config | None = None) -> User:
    """Cria a conta. Quem cadastra já entra — não há email pra confirmar.

    **Isto vaza quais emails têm conta.** Não dá pra evitar sem verificação
    por email: o cadastro tem que dizer se deu certo ou não, e "deu certo"
    significa "esse email não tinha conta".

    O login continua sem vazar nada (mensagem única, tempo constante), que é
    a metade que importa — é lá que alguém tentaria entrar.
    """
    config = config or obter_config()
    email = normalizar_email(email)
    hash_senha = gerar_hash_de_senha(senha)  # valida o tamanho antes de tudo

    if sessao.scalar(select(User).where(User.email == email)) is not None:
        raise EmailJaCadastrado("esse email já tem conta. tente entrar.")

    user = User(
        email=email,
        password_hash=hash_senha,
        piso_ouvintes=config.piso_ouvintes,
        teto_ouvintes=config.teto_ouvintes,
    )
    sessao.add(user)
    sessao.flush()
    return user


def autenticar(sessao: Session, email: str, senha: str) -> User:
    """Confere email e senha em tempo constante.

    Os dois caminhos de falha gastam um argon2 e devolvem a mesma mensagem.
    """
    try:
        email_limpo = normalizar_email(email)
    except EmailInvalido:
        conferir_senha(_hash_dummy(), senha)
        raise CredenciaisInvalidas from None

    user = sessao.scalar(select(User).where(User.email == email_limpo))
    hash_alvo = user.password_hash if user is not None else _hash_dummy()

    if not conferir_senha(hash_alvo, senha) or user is None:
        raise CredenciaisInvalidas

    if hasher().check_needs_rehash(user.password_hash):
        user.password_hash = hasher().hash(senha)
        sessao.flush()

    return user


def trocar_senha(sessao: Session, user: User, nova: str) -> None:
    """Troca a senha e derruba todas as sessões abertas.

    Derrubar é o ponto: se alguém entrou na conta, trocar a senha tem que
    expulsar essa pessoa — senão a sessão dela continua valendo.
    """
    user.password_hash = gerar_hash_de_senha(nova)
    fechar_todas_as_sessoes(sessao, user)
    sessao.flush()


# ---------------------------------------------------------------- sessão


def _hash_token(token: str) -> str:
    """sha256 no token de sessão — e sim, isso está certo.

    A spec proíbe sha256 **pra senha**, porque senha é curta e adivinhável:
    sem argon2, um vazamento do banco vira lista de senhas em horas.

    Token de sessão é outra coisa: são 32 bytes sorteados, 256 bits de
    entropia. Não existe dicionário nem força bruta contra isso, então o
    custo do argon2 não compraria segurança nenhuma.

    E tem um motivo prático: o argon2 usa sal aleatório, então o mesmo token
    gera hashes diferentes toda vez. Não daria pra procurar a sessão pelo
    hash — só varrendo a tabela inteira e verificando linha por linha.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def abrir_sessao_de_login(
    sessao: Session, user: User, user_agent: str | None = None, config: Config | None = None
) -> str:
    """Cria a sessão e devolve o token **em texto puro** — é a única vez que
    ele existe assim. No banco só fica o hash."""
    config = config or obter_config()
    token = secrets.token_urlsafe(TAMANHO_TOKEN_SESSAO)
    sessao.add(
        Session_(
            user_id=user.id,
            token_hash=_hash_token(token),
            expira_em=agora() + timedelta(days=config.sessao_validade_dias),
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    sessao.flush()
    return token


def usuario_por_token(sessao: Session, token: str | None) -> User | None:
    """Quem está logado com esse token, se é que tem alguém."""
    if not token:
        return None
    registro = sessao.scalar(select(Session_).where(Session_.token_hash == _hash_token(token)))
    if registro is None:
        return None
    if agora() > registro.expira_em:
        sessao.delete(registro)  # sessão vencida não serve pra nada
        sessao.flush()
        return None
    return sessao.get(User, registro.user_id)


def fechar_sessao_de_login(sessao: Session, token: str | None) -> bool:
    """Logout de verdade: apaga a sessão no servidor.

    Só limpar o cookie não adiantaria — quem tivesse copiado o token
    continuaria entrando com ele.
    """
    if not token:
        return False
    registro = sessao.scalar(select(Session_).where(Session_.token_hash == _hash_token(token)))
    if registro is None:
        return False
    sessao.delete(registro)
    sessao.flush()
    return True


def fechar_todas_as_sessoes(sessao: Session, user: User) -> int:
    """Derruba o usuário de todos os aparelhos."""
    registros = list(sessao.scalars(select(Session_).where(Session_.user_id == user.id)))
    for registro in registros:
        sessao.delete(registro)
    sessao.flush()
    return len(registros)
