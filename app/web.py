"""Aplicação FastAPI: as rotas de autenticação.

As telas aqui são propositalmente cruas. O visual (fundo quase preto, verde
fósforo, monoespaçada) é a etapa 3 — o que importa agora é o fluxo funcionar
de ponta a ponta.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Annotated

from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import auth
from app.config import Config, obter_config
from app.correio import EmailSender, criar_correio
from app.db import fabrica_de_sessao
from app.garimpo import _sem_a_chave, garimpar, registrar_feedback
from app.lastfm import ClienteLastfm
from app.retrospectiva import pesos_das_pontes, resumo_por_semana
from app.models import (
    Discovery,
    agora,
    Feedback,
    Run,
    SeedArtist,
    StatusRun,
    TipoCodigo,
    User,
    Veredito,
)

MIN_SEMENTES = 3
MAX_SEMENTES = 8

COOKIE_SESSAO = "garimpo_sessao"
COOKIE_CONFIRMANDO = "garimpo_confirmando"

app = FastAPI(
    title="Garimpo",
    docs_url=None,      # é app renderizado no servidor, não API pública
    redoc_url=None,
    openapi_url=None,
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _data_local(momento: datetime | None) -> str:
    """Único lugar do projeto onde fuso horário existe.

    O banco guarda UTC naive (veja `models.agora`); aqui a gente carimba o
    UTC de volta e converte pro fuso de quem está olhando.
    """
    if momento is None:
        return ""
    return momento.replace(tzinfo=timezone.utc).astimezone().strftime("%d/%m/%Y")


templates.env.filters["data"] = _data_local
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


# ---------------------------------------------------------------- dependências


def pegar_sessao():
    """Uma sessão de banco por request, comitada no fim."""
    sessao = fabrica_de_sessao()()
    try:
        yield sessao
        sessao.commit()
    except Exception:
        sessao.rollback()
        raise
    finally:
        sessao.close()


def pegar_config() -> Config:
    return obter_config()


def pegar_correio(config: Annotated[Config, Depends(pegar_config)]) -> EmailSender:
    return criar_correio(config)


def usuario_atual(
    sessao: Annotated[Session, Depends(pegar_sessao)],
    garimpo_sessao: Annotated[str | None, Cookie(alias=COOKIE_SESSAO)] = None,
) -> User | None:
    return auth.usuario_por_token(sessao, garimpo_sessao)


def ip_do_request(request: Request) -> str:
    """Só serve pro rate limit.

    Atrás de proxy isso vira o IP do proxy e o limite por IP perde o efeito;
    quando entrar um proxy na frente, ler o X-Forwarded-For (e só confiar
    nele se o proxy for seu).
    """
    return request.client.host if request.client else "desconhecido"


def pegar_fabrica() -> sessionmaker[Session]:
    """A tarefa de fundo precisa da própria sessão: a do request já foi
    fechada quando ela roda."""
    return fabrica_de_sessao()


def pegar_cliente_lastfm(config: Annotated[Config, Depends(pegar_config)]):
    """Cliente do Last.fm com dono: fecha junto com o request."""
    cliente = ClienteLastfm(config)
    try:
        yield cliente
    finally:
        cliente.fechar()


def pegar_fabrica_lastfm(
    config: Annotated[Config, Depends(pegar_config)],
) -> Callable[[], ClienteLastfm]:
    """Fábrica, não instância: a tarefa de fundo roda depois do request e
    precisa abrir (e fechar) o cliente dela."""
    return lambda: ClienteLastfm(config)


BD = Annotated[Session, Depends(pegar_sessao)]
CFG = Annotated[Config, Depends(pegar_config)]
CORREIO = Annotated[EmailSender, Depends(pegar_correio)]
LOGADO = Annotated[User | None, Depends(usuario_atual)]
FABRICA = Annotated[sessionmaker, Depends(pegar_fabrica)]
LASTFM = Annotated[ClienteLastfm, Depends(pegar_cliente_lastfm)]
FABRICA_LASTFM = Annotated[Callable[[], ClienteLastfm], Depends(pegar_fabrica_lastfm)]


# ---------------------------------------------------------------- helpers


def _tela(request: Request, nome: str, **contexto) -> HTMLResponse:
    return templates.TemplateResponse(request, nome, contexto)


def _gravar_cookie_de_sessao(resposta: Response, token: str, config: Config) -> None:
    resposta.set_cookie(
        COOKIE_SESSAO,
        token,
        httponly=True,                      # JavaScript não enxerga
        samesite="lax",                     # não vai junto em request de outro site
        secure=config.cookie_secure,        # só por HTTPS em produção
        max_age=config.sessao_validade_dias * 24 * 3600,
        path="/",
    )


def _redirecionar(destino: str) -> RedirectResponse:
    """303 porque vem depois de POST: o navegador troca pra GET e o refresh
    não reenvia o formulário."""
    return RedirectResponse(destino, status_code=303)


# ---------------------------------------------------------------- telas


@app.get("/", response_class=HTMLResponse)
def landing(request: Request, usuario: LOGADO):
    if usuario is not None:
        return _redirecionar("/sementes")
    return _tela(request, "landing.html")


@app.get("/cadastro", response_class=HTMLResponse)
def tela_cadastro(request: Request):
    return _tela(request, "cadastro.html")


@app.post("/cadastro")
def fazer_cadastro(
    request: Request,
    sessao: BD,
    config: CFG,
    correio: CORREIO,
    email: Annotated[str, Form()],
    senha: Annotated[str, Form()],
):
    try:
        auth.cadastrar(sessao, email, senha, correio, ip_do_request(request), config)
    except (auth.EmailInvalido, auth.SenhaFraca, auth.LimiteExcedido) as erro:
        return _tela(request, "cadastro.html", erro=str(erro), email=email)

    # o retorno de cadastrar() diz se a conta já existia, mas a resposta é a
    # mesma nos dois casos — de propósito.
    resposta = _redirecionar("/confirmar")
    resposta.set_cookie(
        COOKIE_CONFIRMANDO,
        email.strip().lower(),
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
        max_age=3600,
        path="/",
    )
    return resposta


@app.get("/confirmar", response_class=HTMLResponse)
def tela_confirmar(
    request: Request,
    garimpo_confirmando: Annotated[str | None, Cookie(alias=COOKIE_CONFIRMANDO)] = None,
):
    if not garimpo_confirmando:
        return _redirecionar("/cadastro")
    return _tela(request, "confirmar.html", email=garimpo_confirmando)


@app.post("/confirmar")
def fazer_confirmacao(
    request: Request,
    sessao: BD,
    config: CFG,
    codigo: Annotated[list[str], Form()],
    garimpo_confirmando: Annotated[str | None, Cookie(alias=COOKIE_CONFIRMANDO)] = None,
):
    # a tela manda seis campos de um dígito, todos com name="codigo".
    # juntar aqui é o que faz o formulário funcionar sem javascript.
    codigo_inteiro = "".join(pedaco.strip() for pedaco in codigo)
    if not garimpo_confirmando:
        return _redirecionar("/cadastro")

    usuario = auth.achar_usuario(sessao, garimpo_confirmando)
    if usuario is None:
        # cookie apontando pra conta que não existe: manda recomeçar
        return _redirecionar("/cadastro")

    try:
        auth.conferir_codigo(sessao, usuario, codigo_inteiro, TipoCodigo.CADASTRO, config)
    except auth.ErroDeAuth as erro:
        return _tela(request, "confirmar.html", email=garimpo_confirmando, erro=str(erro))

    token = auth.abrir_sessao_de_login(
        sessao, usuario, request.headers.get("user-agent"), config
    )
    resposta = _redirecionar("/sementes")
    _gravar_cookie_de_sessao(resposta, token, config)
    resposta.delete_cookie(COOKIE_CONFIRMANDO, path="/")
    return resposta


@app.post("/confirmar/reenviar")
def reenviar_codigo(
    request: Request,
    sessao: BD,
    config: CFG,
    correio: CORREIO,
    garimpo_confirmando: Annotated[str | None, Cookie(alias=COOKIE_CONFIRMANDO)] = None,
):
    if not garimpo_confirmando:
        return _redirecionar("/cadastro")

    usuario = auth.achar_usuario(sessao, garimpo_confirmando)
    aviso = "código reenviado. olhe seu email."
    if usuario is not None and not usuario.email_verificado:
        try:
            auth.enviar_codigo(
                sessao, usuario, correio, ip_do_request(request), TipoCodigo.CADASTRO, config
            )
        except auth.LimiteExcedido as erro:
            aviso = str(erro)
    # conta inexistente ou já verificada recebe o mesmo aviso, sem enviar nada
    return _tela(request, "confirmar.html", email=garimpo_confirmando, aviso=aviso)


@app.get("/entrar", response_class=HTMLResponse)
def tela_entrar(request: Request):
    return _tela(request, "entrar.html")


@app.post("/entrar")
def fazer_login(
    request: Request,
    sessao: BD,
    config: CFG,
    email: Annotated[str, Form()],
    senha: Annotated[str, Form()],
):
    try:
        usuario = auth.autenticar(sessao, email, senha)
    except auth.EmailNaoVerificado:
        resposta = _redirecionar("/confirmar")
        resposta.set_cookie(
            COOKIE_CONFIRMANDO,
            email.strip().lower(),
            httponly=True,
            samesite="lax",
            secure=config.cookie_secure,
            max_age=3600,
            path="/",
        )
        return resposta
    except auth.ErroDeAuth as erro:
        return _tela(request, "entrar.html", erro=str(erro), email=email)

    token = auth.abrir_sessao_de_login(
        sessao, usuario, request.headers.get("user-agent"), config
    )
    resposta = _redirecionar("/sementes")
    _gravar_cookie_de_sessao(resposta, token, config)
    return resposta


@app.post("/sair")
def sair(
    sessao: BD,
    garimpo_sessao: Annotated[str | None, Cookie(alias=COOKIE_SESSAO)] = None,
):
    auth.fechar_sessao_de_login(sessao, garimpo_sessao)  # apaga no servidor
    resposta = _redirecionar("/")
    resposta.delete_cookie(COOKIE_SESSAO, path="/")
    return resposta


# ---------------------------------------------------------------- sementes


def _lista_de_sementes(request: Request, usuario: User) -> HTMLResponse:
    return _tela(request, "_sementes_lista.html", usuario=usuario, minimo=MIN_SEMENTES,
                 maximo=MAX_SEMENTES)


@app.get("/sementes", response_class=HTMLResponse)
def tela_sementes(request: Request, usuario: LOGADO):
    if usuario is None:
        return _redirecionar("/entrar")
    return _tela(request, "sementes.html", usuario=usuario, minimo=MIN_SEMENTES,
                 maximo=MAX_SEMENTES)


@app.get("/sementes/buscar", response_class=HTMLResponse)
def buscar_artista(request: Request, usuario: LOGADO, cliente: LASTFM, termo: str = ""):
    """Autocomplete da tela de sementes.

    A busca acontece **no servidor**: a chave do Last.fm nunca chega ao
    navegador.
    """
    if usuario is None:
        return _redirecionar("/entrar")

    termo = termo.strip()
    achados = cliente.buscar(termo, limite=6) if len(termo) >= 2 else []
    ja_tem = {s.nome.lower() for s in usuario.sementes}
    achados = [a for a in achados if a.nome.lower() not in ja_tem]
    return _tela(request, "_sugestoes.html", achados=achados, termo=termo)


@app.post("/sementes/adicionar", response_class=HTMLResponse)
def adicionar_semente(
    request: Request, sessao: BD, usuario: LOGADO, nome: Annotated[str, Form()]
):
    if usuario is None:
        return _redirecionar("/entrar")

    nome = nome.strip()
    ja_tem = {s.nome.lower() for s in usuario.sementes}
    if nome and len(usuario.sementes) < MAX_SEMENTES and nome.lower() not in ja_tem:
        proxima = max((s.ordem for s in usuario.sementes), default=-1) + 1
        sessao.add(SeedArtist(user_id=usuario.id, nome=nome, ordem=proxima))
        sessao.flush()
        sessao.refresh(usuario)
    return _lista_de_sementes(request, usuario)


@app.delete("/sementes/{semente_id}", response_class=HTMLResponse)
def remover_semente(request: Request, sessao: BD, usuario: LOGADO, semente_id: int):
    if usuario is None:
        return _redirecionar("/entrar")

    semente = sessao.get(SeedArtist, semente_id)
    if semente is not None and semente.user_id == usuario.id:
        sessao.delete(semente)
        sessao.flush()
        sessao.refresh(usuario)
    return _lista_de_sementes(request, usuario)


@app.post("/sementes/faixa", response_class=HTMLResponse)
def salvar_faixa(
    request: Request,
    sessao: BD,
    usuario: LOGADO,
    piso: Annotated[int, Form()],
    teto: Annotated[int, Form()],
):
    if usuario is None:
        return _redirecionar("/entrar")

    if piso < 0 or teto <= piso:
        return _tela(request, "_faixa.html", usuario=usuario,
                     erro="o piso precisa ser menor que o teto")
    usuario.piso_ouvintes = piso
    usuario.teto_ouvintes = teto
    sessao.flush()
    return _tela(request, "_faixa.html", usuario=usuario, aviso="faixa salva")


# ---------------------------------------------------------------- histórico


@app.get("/historico", response_class=HTMLResponse)
def tela_historico(request: Request, sessao: BD, usuario: LOGADO):
    """Tudo que foi aprovado, com a rota preservada.

    A rota é o ponto: sem ela isso viraria uma lista de nomes, e daqui a seis
    meses ninguém lembra por que aquele artista apareceu.
    """
    if usuario is None:
        return _redirecionar("/entrar")

    aprovadas = list(
        sessao.scalars(
            select(Discovery)
            .join(Feedback, Feedback.discovery_id == Discovery.id)
            .where(Discovery.user_id == usuario.id, Feedback.veredito == Veredito.APROVADO)
            .order_by(Feedback.criado_em.desc())
        )
    )
    return _tela(request, "historico.html", usuario=usuario, aprovadas=aprovadas,
                 secao="historico")


# ---------------------------------------------------------------- retrospectiva


@app.get("/retrospectiva", response_class=HTMLResponse)
def tela_retrospectiva(request: Request, sessao: BD, usuario: LOGADO):
    """O que rendeu por semana, e o peso que cada ponte tem agora.

    A segunda metade é o loop de aprendizado ficando visível: são os mesmos
    números que o motor usa pra pontuar a próxima rodada.
    """
    if usuario is None:
        return _redirecionar("/entrar")

    return _tela(
        request,
        "retrospectiva.html",
        usuario=usuario,
        semanas=resumo_por_semana(sessao, usuario),
        pesos=pesos_das_pontes(sessao, usuario),
        secao="retrospectiva",
    )


# ---------------------------------------------------------------- garimpo


def _rodada_em_andamento(sessao: Session, usuario: User) -> Run | None:
    return sessao.scalar(
        select(Run)
        .where(Run.user_id == usuario.id, Run.status == StatusRun.RODANDO)
        .order_by(Run.iniciado_em.desc())
    )


def _ultima_rodada(sessao: Session, usuario: User) -> Run | None:
    return sessao.scalar(
        select(Run).where(Run.user_id == usuario.id).order_by(Run.iniciado_em.desc())
    )


def _pendentes(sessao: Session, usuario: User) -> list[Discovery]:
    """Descobertas ainda sem veredito, da melhor pra pior."""
    return list(
        sessao.scalars(
            select(Discovery)
            .outerjoin(Feedback, Feedback.discovery_id == Discovery.id)
            .where(Discovery.user_id == usuario.id, Feedback.id.is_(None))
            .order_by(Discovery.score.desc())
        )
    )


def _estado(request: Request, sessao: Session, usuario: User) -> HTMLResponse:
    """O miolo da tela de garimpo. Serve tanto pro carregamento inicial
    quanto pra cada resposta do polling e de cada botão."""
    rodada = _rodada_em_andamento(sessao, usuario)
    pendentes = _pendentes(sessao, usuario)
    ultima = _ultima_rodada(sessao, usuario)
    return _tela(
        request,
        "_garimpo_estado.html",
        usuario=usuario,
        rodando=rodada is not None,
        pendentes=pendentes,
        total=len(pendentes),
        erro_da_rodada=(ultima.erro if ultima and ultima.status == StatusRun.ERRO else None),
        minimo=MIN_SEMENTES,
    )


def rodar_em_segundo_plano(
    fabrica: sessionmaker,
    fabrica_lastfm: Callable[[], ClienteLastfm],
    user_id: int,
    rodada_id: int,
) -> None:
    """O garimpo fora do request.

    São centenas de chamadas ao Last.fm — a rodada real levou 72s. Nenhum
    navegador espera isso. O `garimpar()` já grava status e erro na tabela
    `runs`, que é o que o polling lê.
    """
    cliente = fabrica_lastfm()
    try:
        with fabrica() as sessao:
            usuario = sessao.get(User, user_id)
            if usuario is None:
                return
            try:
                garimpar(sessao, usuario, cliente=cliente, rodada_id=rodada_id)
                sessao.commit()
                return
            except Exception as erro:
                sessao.rollback()
                falha = erro
    finally:
        cliente.fechar()

    # Sessão nova pra registrar a falha: a anterior pode estar inutilizável
    # (um erro de flush deixa ela em PendingRollbackError). Se ninguém marcar
    # a rodada como terminada, a tela fica em "garimpando" pra sempre.
    with fabrica() as sessao:
        rodada = sessao.get(Run, rodada_id)
        if rodada is not None and rodada.status == StatusRun.RODANDO:
            rodada.status = StatusRun.ERRO
            rodada.erro = _sem_a_chave(f"{type(falha).__name__}: {falha}", obter_config())
            rodada.terminado_em = agora()
            sessao.commit()

@app.get("/garimpo", response_class=HTMLResponse)
def tela_garimpo(request: Request, usuario: LOGADO):
    if usuario is None:
        return _redirecionar("/entrar")
    return _tela(request, "garimpo.html", usuario=usuario, minimo=MIN_SEMENTES)


@app.get("/garimpo/estado", response_class=HTMLResponse)
def estado_do_garimpo(request: Request, sessao: BD, usuario: LOGADO):
    """Alvo do polling do HTMX enquanto a rodada não termina."""
    if usuario is None:
        return _redirecionar("/entrar")
    return _estado(request, sessao, usuario)


@app.post("/garimpo", response_class=HTMLResponse)
def comecar_garimpo(
    request: Request,
    sessao: BD,
    usuario: LOGADO,
    fabrica: FABRICA,
    fabrica_lastfm: FABRICA_LASTFM,
    tarefas: BackgroundTasks,
):
    if usuario is None:
        return _redirecionar("/entrar")

    if len(usuario.sementes) < MIN_SEMENTES:
        return _estado(request, sessao, usuario)
    if _rodada_em_andamento(sessao, usuario) is not None:
        return _estado(request, sessao, usuario)  # já tem uma rodando

    rodada = Run(user_id=usuario.id, status=StatusRun.RODANDO)
    sessao.add(rodada)
    sessao.flush()
    rodada_id = rodada.id

    # commit AQUI, antes de agendar. a tarefa de fundo abre outra sessão e
    # precisa (a) enxergar esta linha e (b) achar o banco destravado. sem
    # isso ela criava uma segunda rodada e batia em `database is locked`.
    sessao.commit()

    tarefas.add_task(rodar_em_segundo_plano, fabrica, fabrica_lastfm, usuario.id, rodada_id)
    return _estado(request, sessao, usuario)


@app.post("/garimpo/{descoberta_id}/{veredito}", response_class=HTMLResponse)
def dar_veredito(
    request: Request, sessao: BD, usuario: LOGADO, descoberta_id: int, veredito: str
):
    """aprovar / pular / nunca mais. Devolve só o próximo card — a página
    não recarrega."""
    if usuario is None:
        return _redirecionar("/entrar")

    try:
        escolha = Veredito(veredito)
    except ValueError:
        return _estado(request, sessao, usuario)

    descoberta = sessao.get(Discovery, descoberta_id)
    if descoberta is not None and descoberta.user_id == usuario.id:
        registrar_feedback(sessao, descoberta, escolha)
    return _estado(request, sessao, usuario)
