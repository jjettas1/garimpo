"""Autenticação: senha, código por email, sessão e rate limit.

Feito na mão, sem biblioteca de auth pronta. As decisões que parecem
estranhas estão comentadas onde acontecem — quase todas existem pra evitar
que a aplicação conte pra um desconhecido quais emails têm conta aqui.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Config, obter_config
from app.correio import EmailSender, texto_do_codigo
from app.models import (
    EnvioCodigo,
    Session_,
    TipoCodigo,
    User,
    VerificationCode,
    agora,
)

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


class EmailNaoVerificado(ErroDeAuth):
    pass


class SenhaFraca(ErroDeAuth):
    pass


class EmailInvalido(ErroDeAuth):
    pass


class CodigoInvalido(ErroDeAuth):
    pass


class CodigoExpirado(ErroDeAuth):
    pass


class CodigoBloqueado(ErroDeAuth):
    """Queimou as tentativas. Só pedindo outro código."""


class LimiteExcedido(ErroDeAuth):
    pass


# ---------------------------------------------------------------- senha


@lru_cache(maxsize=1)
def hasher(config: Config | None = None) -> PasswordHasher:
    """argon2id, com os parâmetros vindos da config.

    O PasswordHasher do argon2-cffi já usa Type.ID por padrão, mas deixo
    explícito: argon2**i** e argon2**d** não servem aqui.
    """
    from argon2 import Type

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


def achar_usuario(sessao: Session, email: str) -> User | None:
    try:
        return sessao.scalar(select(User).where(User.email == normalizar_email(email)))
    except EmailInvalido:
        return None


# ---------------------------------------------------------------- rate limit


def _contar_envios(sessao: Session, coluna, valor: str, desde) -> int:
    return sessao.scalar(
        select(func.count(EnvioCodigo.id)).where(coluna == valor, EnvioCodigo.criado_em >= desde)
    )


def checar_limite_de_envio(sessao: Session, email: str, ip: str, config: Config) -> None:
    """3 por email por hora, 10 por IP por hora. Em tabela, sem Redis.

    O limite por email impede encher a caixa de uma pessoa; o limite por IP
    impede varrer emails atrás de quem tem conta.
    """
    desde = agora() - timedelta(hours=1)

    if _contar_envios(sessao, EnvioCodigo.email, email, desde) >= config.limite_codigo_por_email_hora:
        raise LimiteExcedido("muitos códigos pedidos pra esse email. tente de novo em uma hora.")

    if _contar_envios(sessao, EnvioCodigo.ip, ip, desde) >= config.limite_codigo_por_ip_hora:
        raise LimiteExcedido("muitos códigos pedidos daqui. tente de novo em uma hora.")


def registrar_envio(sessao: Session, email: str, ip: str) -> None:
    sessao.add(EnvioCodigo(email=email, ip=ip))
    sessao.flush()


# ---------------------------------------------------------------- código


def sortear_codigo() -> str:
    """Seis dígitos, do gerador criptográfico.

    `secrets`, não `random`: o `random` é previsível se alguém observar
    saídas suficientes.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


def enviar_codigo(
    sessao: Session,
    user: User,
    correio: EmailSender,
    ip: str,
    tipo: TipoCodigo = TipoCodigo.CADASTRO,
    config: Config | None = None,
) -> str:
    """Sorteia um código, guarda o **hash** dele e manda o email.

    Devolve o código em texto puro só pra quem chamou poder testar; em
    produção ninguém usa esse retorno.
    """
    config = config or obter_config()
    checar_limite_de_envio(sessao, user.email, ip, config)

    # códigos anteriores morrem: se o usuário pediu reenvio, só o novo vale
    for antigo in sessao.scalars(
        select(VerificationCode).where(
            VerificationCode.user_id == user.id,
            VerificationCode.tipo == tipo,
            VerificationCode.usado_em.is_(None),
        )
    ):
        antigo.usado_em = agora()

    codigo = sortear_codigo()
    sessao.add(
        VerificationCode(
            user_id=user.id,
            codigo_hash=hasher().hash(codigo),
            tipo=tipo,
            expira_em=agora() + timedelta(minutes=config.codigo_validade_minutos),
            tentativas=0,
        )
    )
    registrar_envio(sessao, user.email, ip)
    sessao.flush()

    assunto, corpo = texto_do_codigo(codigo, config.codigo_validade_minutos)
    correio.enviar(user.email, assunto, corpo)
    return codigo


def conferir_codigo(
    sessao: Session,
    user: User,
    codigo: str,
    tipo: TipoCodigo = TipoCodigo.CADASTRO,
    config: Config | None = None,
) -> None:
    """Confere o código. Silêncio quando dá certo, exceção quando não.

    O código é de uso único: acertar duas vezes seguidas não funciona.
    """
    config = config or obter_config()
    registro = sessao.scalar(
        select(VerificationCode)
        .where(
            VerificationCode.user_id == user.id,
            VerificationCode.tipo == tipo,
            VerificationCode.usado_em.is_(None),
        )
        .order_by(VerificationCode.criado_em.desc())
    )
    if registro is None:
        raise CodigoInvalido("código inválido")

    if agora() > registro.expira_em:
        raise CodigoExpirado("esse código expirou. peça outro.")

    if registro.tentativas >= config.codigo_max_tentativas:
        raise CodigoBloqueado("código bloqueado por tentativas demais. peça outro.")

    if not conferir_senha(registro.codigo_hash, codigo):
        registro.tentativas += 1
        # na última tentativa errada o código morre junto, pra não sobrar
        # nem mais um chute
        if registro.tentativas >= config.codigo_max_tentativas:
            registro.usado_em = agora()
            sessao.flush()
            raise CodigoBloqueado("código bloqueado por tentativas demais. peça outro.")
        sessao.flush()
        raise CodigoInvalido("código inválido")

    registro.usado_em = agora()
    if tipo == TipoCodigo.CADASTRO:
        user.email_verificado = True
    sessao.flush()


# ---------------------------------------------------------------- cadastro


def cadastrar(
    sessao: Session,
    email: str,
    senha: str,
    correio: EmailSender,
    ip: str,
    config: Config | None = None,
) -> User | None:
    """Cria a conta e manda o código. Devolve None quando o email já é de
    alguém verificado.

    O None é de propósito: quem chamou responde a mesma coisa nos dois casos.
    Um cadastro que responde "email já em uso" desmonta toda a proteção do
    login — bastaria tentar cadastrar pra descobrir quem tem conta.
    """
    config = config or obter_config()
    email = normalizar_email(email)
    hash_senha = gerar_hash_de_senha(senha)  # valida o tamanho antes de tudo

    existente = sessao.scalar(select(User).where(User.email == email))

    if existente is not None and existente.email_verificado:
        # avisa o dono de verdade, e não cria nada
        checar_limite_de_envio(sessao, email, ip, config)
        registrar_envio(sessao, email, ip)
        correio.enviar(
            email,
            "Alguém tentou criar uma conta no Garimpo com o seu email",
            "Se foi você, é só entrar normalmente — você já tem conta.\n"
            "Se não foi, pode ignorar: nada mudou na sua conta.",
        )
        sessao.flush()
        return None

    if existente is not None:
        # cadastro que ficou pela metade: troca a senha e manda outro código.
        # seguro porque continuar exige o código, que vai pro dono do email.
        existente.password_hash = hash_senha
        enviar_codigo(sessao, existente, correio, ip, TipoCodigo.CADASTRO, config)
        return existente

    user = User(
        email=email,
        password_hash=hash_senha,
        email_verificado=False,
        piso_ouvintes=config.piso_ouvintes,
        teto_ouvintes=config.teto_ouvintes,
    )
    sessao.add(user)
    sessao.flush()
    enviar_codigo(sessao, user, correio, ip, TipoCodigo.CADASTRO, config)
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

    # só depois da senha conferir é que dá pra falar de email não verificado.
    # quem chegou aqui já provou que sabe a senha, então não há o que vazar.
    if not user.email_verificado:
        raise EmailNaoVerificado("confirme seu email antes de entrar")

    if hasher().check_needs_rehash(user.password_hash):
        user.password_hash = hasher().hash(senha)
        sessao.flush()

    return user


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
    """Derruba o usuário de todos os aparelhos. Usado depois de troca de senha."""
    registros = list(sessao.scalars(select(Session_).where(Session_.user_id == user.id)))
    for registro in registros:
        sessao.delete(registro)
    sessao.flush()
    return len(registros)
