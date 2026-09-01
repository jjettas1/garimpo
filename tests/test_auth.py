"""Testes de autenticação.

A spec obriga a cobrir a expiração e a invalidação do código e o rate limit.
Cobri também as duas coisas que quebram calado se ninguém olhar: a não
enumeração de usuários e o logout de verdade.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import auth
from app.models import Session_, TipoCodigo, User, VerificationCode, agora

IP = "10.0.0.1"


# ------------------------------------------------------------ senha


def test_hash_e_argon2id_mesmo(config):
    """Não é argon2i nem argon2d. A spec é específica e o motivo é real:
    o id é o único que resiste aos dois tipos de ataque."""
    assert auth.gerar_hash_de_senha("senha-boa-123").startswith("$argon2id$")


def test_senha_certa_confere_e_errada_nao():
    guardado = auth.gerar_hash_de_senha("senha-boa-123")
    assert auth.conferir_senha(guardado, "senha-boa-123")
    assert not auth.conferir_senha(guardado, "senha-boa-124")


def test_mesma_senha_gera_hashes_diferentes():
    """Sal aleatório. Sem isso, hashes iguais no banco entregariam quem usa
    a mesma senha que quem."""
    a = auth.gerar_hash_de_senha("senha-boa-123")
    b = auth.gerar_hash_de_senha("senha-boa-123")
    assert a != b


def test_senha_curta_e_recusada():
    with pytest.raises(auth.SenhaFraca):
        auth.gerar_hash_de_senha("curta")


def test_hash_estragado_nao_estoura():
    """Conta criada pela CLI tem hash inválido. Isso não pode derrubar o login."""
    assert not auth.conferir_senha("!sem-senha-ainda", "qualquer-coisa")


# ------------------------------------------------------------ email


@pytest.mark.parametrize(
    "entrada,esperado",
    [("  Eu@Exemplo.COM ", "eu@exemplo.com"), ("a@b.co", "a@b.co")],
)
def test_email_normalizado(entrada, esperado):
    assert auth.normalizar_email(entrada) == esperado


@pytest.mark.parametrize("ruim", ["sem-arroba", "@sodominio.com", "eu@", "eu@semponto", "a@b@c.com"])
def test_email_invalido_e_recusado(ruim):
    with pytest.raises(auth.EmailInvalido):
        auth.normalizar_email(ruim)


def test_caixa_diferente_nao_vira_conta_nova(sessao, correio, config):
    auth.cadastrar(sessao, "Eu@Exemplo.com", "senha-boa-123", correio, IP, config)
    auth.cadastrar(sessao, "eu@exemplo.com", "outra-senha-99", correio, IP, config)
    assert sessao.scalar(select(User).where(User.email == "eu@exemplo.com")) is not None
    assert len(sessao.scalars(select(User)).all()) == 1


# ------------------------------------------------------------ código


def test_codigo_tem_sempre_seis_digitos():
    for _ in range(200):
        codigo = auth.sortear_codigo()
        assert len(codigo) == 6 and codigo.isdigit()


def test_codigo_nao_fica_em_texto_puro_no_banco(sessao, correio, config):
    user = auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)
    codigo = correio.ultimo_codigo

    registro = sessao.scalar(select(VerificationCode).where(VerificationCode.user_id == user.id))
    assert codigo not in registro.codigo_hash
    assert registro.codigo_hash.startswith("$argon2id$")


def test_codigo_certo_verifica_o_email(sessao, correio, config):
    user = auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)
    assert not user.email_verificado

    auth.conferir_codigo(sessao, user, correio.ultimo_codigo, config=config)
    assert user.email_verificado


def test_codigo_expira_em_quinze_minutos(sessao, correio, config):
    user = auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)
    codigo = correio.ultimo_codigo

    registro = sessao.scalar(select(VerificationCode).where(VerificationCode.user_id == user.id))
    # o relógio anda 16 minutos
    registro.expira_em = agora() - timedelta(minutes=1)
    sessao.flush()

    with pytest.raises(auth.CodigoExpirado):
        auth.conferir_codigo(sessao, user, codigo, config=config)
    assert not user.email_verificado


def test_prazo_do_codigo_e_o_da_config(sessao, correio, config):
    user = auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)
    registro = sessao.scalar(select(VerificationCode).where(VerificationCode.user_id == user.id))
    faltando = (registro.expira_em - agora()).total_seconds() / 60
    assert config.codigo_validade_minutos - 1 < faltando <= config.codigo_validade_minutos


def test_cinco_erros_queimam_o_codigo(sessao, correio, config):
    user = auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)
    codigo = correio.ultimo_codigo
    errado = "000000" if codigo != "000000" else "111111"

    for _ in range(config.codigo_max_tentativas - 1):
        with pytest.raises(auth.CodigoInvalido):
            auth.conferir_codigo(sessao, user, errado, config=config)

    with pytest.raises(auth.CodigoBloqueado):  # a quinta bloqueia
        auth.conferir_codigo(sessao, user, errado, config=config)

    # e depois de queimado, nem o código certo abre
    with pytest.raises(auth.ErroDeAuth):
        auth.conferir_codigo(sessao, user, codigo, config=config)
    assert not user.email_verificado


def test_codigo_e_de_uso_unico(sessao, correio, config):
    user = auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)
    codigo = correio.ultimo_codigo

    auth.conferir_codigo(sessao, user, codigo, config=config)
    with pytest.raises(auth.ErroDeAuth):
        auth.conferir_codigo(sessao, user, codigo, config=config)


def test_reenviar_mata_o_codigo_anterior(sessao, correio, config):
    user = auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)
    primeiro = correio.ultimo_codigo

    auth.enviar_codigo(sessao, user, correio, IP, TipoCodigo.CADASTRO, config)
    segundo = correio.ultimo_codigo

    with pytest.raises(auth.ErroDeAuth):
        auth.conferir_codigo(sessao, user, primeiro, config=config)
    auth.conferir_codigo(sessao, user, segundo, config=config)
    assert user.email_verificado


# ------------------------------------------------------------ rate limit


def test_limite_de_tres_codigos_por_email_por_hora(sessao, correio, config):
    user = auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)  # 1º envio

    auth.enviar_codigo(sessao, user, correio, IP, config=config)  # 2º
    auth.enviar_codigo(sessao, user, correio, IP, config=config)  # 3º

    with pytest.raises(auth.LimiteExcedido):
        auth.enviar_codigo(sessao, user, correio, IP, config=config)  # 4º


def test_limite_de_dez_por_ip_mesmo_com_emails_diferentes(sessao, correio, config):
    """O limite por email não segura quem varre emails atrás de quem tem
    conta — pra isso é o limite por IP."""
    for n in range(config.limite_codigo_por_ip_hora):
        auth.cadastrar(sessao, f"pessoa{n}@b.com", "senha-boa-123", correio, IP, config)

    with pytest.raises(auth.LimiteExcedido):
        auth.cadastrar(sessao, "gota-dagua@b.com", "senha-boa-123", correio, IP, config)


def test_ips_diferentes_nao_atrapalham_um_ao_outro(sessao, correio, config):
    for n in range(config.limite_codigo_por_ip_hora):
        auth.cadastrar(sessao, f"pessoa{n}@b.com", "senha-boa-123", correio, "10.0.0.1", config)

    # outro IP continua livre
    auth.cadastrar(sessao, "outra@b.com", "senha-boa-123", correio, "10.0.0.2", config)


def test_envio_de_mais_de_uma_hora_atras_nao_conta(sessao, correio, config):
    from app.models import EnvioCodigo

    user = auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)
    auth.enviar_codigo(sessao, user, correio, IP, config=config)
    auth.enviar_codigo(sessao, user, correio, IP, config=config)

    # a janela é deslizante: envelhece tudo em duas horas
    for envio in sessao.scalars(select(EnvioCodigo)):
        envio.criado_em = agora() - timedelta(hours=2)
    sessao.flush()

    auth.enviar_codigo(sessao, user, correio, IP, config=config)  # não estoura


# ------------------------------------------------------------ sem enumeração


def test_email_inexistente_e_senha_errada_dao_a_mesma_mensagem(sessao, cadastrado):
    with pytest.raises(auth.CredenciaisInvalidas) as sem_conta:
        auth.autenticar(sessao, "ninguem@exemplo.com", "senha-boa-123")

    with pytest.raises(auth.CredenciaisInvalidas) as senha_errada:
        auth.autenticar(sessao, cadastrado.email, "senha-errada-99")

    a, b = str(sem_conta.value), str(senha_errada.value)
    assert a == b, "as mensagens precisam ser idênticas"
    assert a.encode() == b.encode(), "byte a byte"


def test_os_dois_caminhos_gastam_um_argon2_cada(sessao, cadastrado, monkeypatch):
    """Mensagem igual não basta: se o caminho 'email não existe' respondesse
    sem calcular hash nenhum, o cronômetro entregaria quem tem conta.

    Contar as chamadas é mais confiável que medir tempo — não depende de
    quão ocupada a máquina está na hora.
    """
    chamadas = []
    original = auth.conferir_senha
    monkeypatch.setattr(
        auth, "conferir_senha", lambda h, s: (chamadas.append(h), original(h, s))[1]
    )

    with pytest.raises(auth.CredenciaisInvalidas):
        auth.autenticar(sessao, "ninguem@exemplo.com", "chute")
    sem_conta = len(chamadas)

    chamadas.clear()
    with pytest.raises(auth.CredenciaisInvalidas):
        auth.autenticar(sessao, cadastrado.email, "chute")
    com_conta = len(chamadas)

    assert sem_conta == com_conta == 1


def test_email_mal_formado_tambem_gasta_o_hash(sessao, monkeypatch):
    """Senão 'não tem arroba' responderia na hora e viraria outro sinal."""
    chamadas = []
    original = auth.conferir_senha
    monkeypatch.setattr(
        auth, "conferir_senha", lambda h, s: (chamadas.append(h), original(h, s))[1]
    )
    with pytest.raises(auth.CredenciaisInvalidas):
        auth.autenticar(sessao, "isso-nao-e-email", "chute")
    assert len(chamadas) == 1


def test_cadastrar_email_ja_usado_nao_cria_conta_nem_denuncia(sessao, cadastrado, correio, config):
    resultado = auth.cadastrar(sessao, cadastrado.email, "outra-senha-99", correio, IP, config)

    assert resultado is None  # quem chamou responde igual ao caso normal
    assert len(sessao.scalars(select(User)).all()) == 1
    assert not auth.conferir_senha(cadastrado.password_hash, "outra-senha-99")


def test_dono_do_email_e_avisado_da_tentativa(sessao, cadastrado, correio, config):
    auth.cadastrar(sessao, cadastrado.email, "outra-senha-99", correio, IP, config)

    assert correio.destinos == [cadastrado.email]
    assert "tentou criar uma conta" in correio.enviados[-1][1].lower()


def test_cadastro_pela_metade_pode_ser_retomado(sessao, correio, config):
    """Quem errou a senha no cadastro e não confirmou não fica preso pra sempre."""
    auth.cadastrar(sessao, "a@b.com", "senha-errada-1", correio, IP, config)
    user = auth.cadastrar(sessao, "a@b.com", "senha-nova-123", correio, IP, config)

    assert user is not None
    auth.conferir_codigo(sessao, user, correio.ultimo_codigo, config=config)
    assert auth.autenticar(sessao, "a@b.com", "senha-nova-123") is user


def test_conta_nao_verificada_so_revela_isso_depois_da_senha_certa(sessao, correio, config):
    auth.cadastrar(sessao, "a@b.com", "senha-boa-123", correio, IP, config)

    # senha errada: mensagem genérica, não conta que a conta existe
    with pytest.raises(auth.CredenciaisInvalidas):
        auth.autenticar(sessao, "a@b.com", "chute-errado-9")

    # senha certa: agora pode falar, quem chegou aqui já provou que é dono
    with pytest.raises(auth.EmailNaoVerificado):
        auth.autenticar(sessao, "a@b.com", "senha-boa-123")


def test_login_certo_devolve_o_usuario(sessao, cadastrado):
    assert auth.autenticar(sessao, "  JAIME@exemplo.com ", "senha-boa-123") is cadastrado


# ------------------------------------------------------------ sessão


def test_token_nao_fica_em_texto_puro_no_banco(sessao, cadastrado):
    token = auth.abrir_sessao_de_login(sessao, cadastrado)

    registro = sessao.scalar(select(Session_))
    assert registro.token_hash != token
    assert token not in registro.token_hash


def test_token_valido_encontra_o_usuario(sessao, cadastrado):
    token = auth.abrir_sessao_de_login(sessao, cadastrado)
    assert auth.usuario_por_token(sessao, token) is cadastrado


@pytest.mark.parametrize("ruim", [None, "", "token-inventado"])
def test_token_ruim_nao_encontra_ninguem(sessao, cadastrado, ruim):
    auth.abrir_sessao_de_login(sessao, cadastrado)
    assert auth.usuario_por_token(sessao, ruim) is None


def test_sessao_vencida_nao_vale_e_some(sessao, cadastrado):
    token = auth.abrir_sessao_de_login(sessao, cadastrado)
    registro = sessao.scalar(select(Session_))
    registro.expira_em = agora() - timedelta(seconds=1)
    sessao.flush()

    assert auth.usuario_por_token(sessao, token) is None
    assert sessao.scalar(select(Session_)) is None


def test_logout_apaga_a_sessao_no_servidor(sessao, cadastrado):
    """Se só limpasse o cookie, quem tivesse copiado o token continuaria dentro."""
    token = auth.abrir_sessao_de_login(sessao, cadastrado)

    assert auth.fechar_sessao_de_login(sessao, token)
    assert sessao.scalar(select(Session_)) is None
    assert auth.usuario_por_token(sessao, token) is None


def test_cada_login_gera_um_token_diferente(sessao, cadastrado):
    a = auth.abrir_sessao_de_login(sessao, cadastrado)
    b = auth.abrir_sessao_de_login(sessao, cadastrado)
    assert a != b
    assert auth.usuario_por_token(sessao, a) is cadastrado
    assert auth.usuario_por_token(sessao, b) is cadastrado


def test_derrubar_todas_as_sessoes(sessao, cadastrado):
    a = auth.abrir_sessao_de_login(sessao, cadastrado)
    b = auth.abrir_sessao_de_login(sessao, cadastrado)

    assert auth.fechar_todas_as_sessoes(sessao, cadastrado) == 2
    assert auth.usuario_por_token(sessao, a) is None
    assert auth.usuario_por_token(sessao, b) is None


# ------------------------------------------------------------ pelas telas


def test_fluxo_completo_pelo_navegador(cliente_web, correio):
    r = cliente_web.post("/cadastro", data={"email": "novo@exemplo.com", "senha": "senha-boa-123"})
    assert r.status_code == 200 and r.url.path == "/confirmar"

    r = cliente_web.post("/confirmar", data={"codigo": correio.ultimo_codigo})
    assert r.url.path == "/sementes"
    assert 'action="/sair"' in r.text  # o botão de sair só aparece logado

    r = cliente_web.post("/sair")
    assert r.url.path == "/"

    r = cliente_web.get("/sementes")
    assert r.url.path == "/entrar"  # sem sessão, volta pro login


def test_cookie_de_sessao_e_protegido(cliente_web, correio):
    cliente_web.post("/cadastro", data={"email": "novo@exemplo.com", "senha": "senha-boa-123"})
    resposta = cliente_web.post(
        "/confirmar", data={"codigo": correio.ultimo_codigo}, follow_redirects=False
    )

    bruto = resposta.headers["set-cookie"].lower()
    assert "httponly" in bruto        # JavaScript não lê
    assert "samesite=lax" in bruto    # não vaza pra outro site
    assert "secure" not in bruto      # em dev não tem HTTPS; em prod tem


def test_login_errado_nao_diz_qual_dos_dois_errou(cliente_web):
    r = cliente_web.post("/entrar", data={"email": "ninguem@x.com", "senha": "chute-errado"})
    assert auth.MENSAGEM_CREDENCIAL_INVALIDA in r.text
    assert "não cadastrado" not in r.text


def test_landing_e_publica(cliente_web):
    assert cliente_web.get("/").status_code == 200


def test_nao_expoe_documentacao_de_api(cliente_web):
    """A spec proíbe OpenAPI voltada pra terceiros."""
    for rota in ("/docs", "/redoc", "/openapi.json"):
        assert cliente_web.get(rota).status_code == 404
