"""Testes de autenticação.

Não há verificação por email: cadastrar entra direto. O que precisa continuar
de pé é o hash da senha, a não enumeração **no login** e a sessão.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import auth
from app.models import Session_, User, agora


# ------------------------------------------------------------ senha


def test_hash_e_argon2id_mesmo():
    """Não é argon2i nem argon2d. O id é o único que resiste aos dois tipos
    de ataque."""
    assert auth.gerar_hash_de_senha("senha-boa-123").startswith("$argon2id$")


def test_senha_certa_confere_e_errada_nao():
    guardado = auth.gerar_hash_de_senha("senha-boa-123")
    assert auth.conferir_senha(guardado, "senha-boa-123")
    assert not auth.conferir_senha(guardado, "senha-boa-124")


def test_mesma_senha_gera_hashes_diferentes():
    """Sal aleatório. Sem isso, hashes iguais no banco entregariam quem usa
    a mesma senha que quem."""
    assert auth.gerar_hash_de_senha("senha-boa-123") != auth.gerar_hash_de_senha("senha-boa-123")


def test_senha_curta_e_recusada():
    with pytest.raises(auth.SenhaFraca):
        auth.gerar_hash_de_senha("curta")


def test_hash_estragado_nao_estoura():
    """Conta criada pela CLI sem senha tem hash inválido. Isso não pode
    derrubar o login."""
    assert not auth.conferir_senha("!sem-senha", "qualquer-coisa")


# ------------------------------------------------------------ email


@pytest.mark.parametrize(
    "entrada,esperado", [("  Eu@Exemplo.COM ", "eu@exemplo.com"), ("a@b.co", "a@b.co")]
)
def test_email_normalizado(entrada, esperado):
    assert auth.normalizar_email(entrada) == esperado


@pytest.mark.parametrize("ruim", ["sem-arroba", "@sodominio.com", "eu@", "eu@semponto", "a@b@c.com"])
def test_email_invalido_e_recusado(ruim):
    with pytest.raises(auth.EmailInvalido):
        auth.normalizar_email(ruim)


# ------------------------------------------------------------ cadastro


def test_cadastro_cria_a_conta_pronta_pra_entrar(sessao, config):
    user = auth.cadastrar(sessao, "novo@exemplo.com", "senha-boa-123", config)

    assert user.email == "novo@exemplo.com"
    assert auth.autenticar(sessao, "novo@exemplo.com", "senha-boa-123") is user


def test_cadastro_herda_a_faixa_padrao_de_ouvintes(sessao, config):
    user = auth.cadastrar(sessao, "novo@exemplo.com", "senha-boa-123", config)

    assert user.piso_ouvintes == config.piso_ouvintes
    assert user.teto_ouvintes == config.teto_ouvintes


def test_email_repetido_e_recusado(sessao, config, cadastrado):
    with pytest.raises(auth.EmailJaCadastrado):
        auth.cadastrar(sessao, cadastrado.email, "outra-senha-99", config)

    assert len(sessao.scalars(select(User)).all()) == 1
    assert auth.conferir_senha(cadastrado.password_hash, "senha-boa-123")  # senha intacta


def test_caixa_diferente_nao_vira_conta_nova(sessao, config):
    auth.cadastrar(sessao, "Eu@Exemplo.com", "senha-boa-123", config)
    with pytest.raises(auth.EmailJaCadastrado):
        auth.cadastrar(sessao, "eu@exemplo.com", "outra-senha-99", config)

    assert len(sessao.scalars(select(User)).all()) == 1


def test_cadastro_com_senha_curta_nao_cria_nada(sessao, config):
    with pytest.raises(auth.SenhaFraca):
        auth.cadastrar(sessao, "novo@exemplo.com", "curta", config)

    assert sessao.scalars(select(User)).all() == []


# ------------------------------------------------------------ login


def test_login_certo_devolve_o_usuario(sessao, cadastrado):
    assert auth.autenticar(sessao, "  JAIME@exemplo.com ", "senha-boa-123") is cadastrado


def test_email_inexistente_e_senha_errada_dao_a_mesma_mensagem(sessao, cadastrado):
    """A metade que importa da não enumeração. O cadastro vaza (não tem como
    não vazar sem email), mas o login é onde alguém tentaria entrar."""
    with pytest.raises(auth.CredenciaisInvalidas) as sem_conta:
        auth.autenticar(sessao, "ninguem@exemplo.com", "senha-boa-123")

    with pytest.raises(auth.CredenciaisInvalidas) as senha_errada:
        auth.autenticar(sessao, cadastrado.email, "senha-errada-99")

    a, b = str(sem_conta.value), str(senha_errada.value)
    assert a == b and a.encode() == b.encode(), "as mensagens precisam ser idênticas byte a byte"


def test_os_dois_caminhos_gastam_um_argon2_cada(sessao, cadastrado, monkeypatch):
    """Mensagem igual não basta: se o caminho 'email não existe' respondesse
    sem calcular hash nenhum, o cronômetro entregaria quem tem conta.

    Contar as chamadas é mais confiável que medir tempo — não depende de
    quão ocupada a máquina está na hora.
    """
    chamadas: list[str] = []
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

    assert sem_conta == len(chamadas) == 1


def test_email_mal_formado_tambem_gasta_o_hash(sessao, monkeypatch):
    """Senão 'não tem arroba' responderia na hora e viraria outro sinal."""
    chamadas: list[str] = []
    original = auth.conferir_senha
    monkeypatch.setattr(
        auth, "conferir_senha", lambda h, s: (chamadas.append(h), original(h, s))[1]
    )

    with pytest.raises(auth.CredenciaisInvalidas):
        auth.autenticar(sessao, "isso-nao-e-email", "chute")

    assert len(chamadas) == 1


# ------------------------------------------------------------ troca de senha


def test_trocar_senha_derruba_as_sessoes_abertas(sessao, cadastrado):
    """Se alguém entrou na conta, trocar a senha tem que expulsar essa
    pessoa — senão a sessão dela continua valendo."""
    token = auth.abrir_sessao_de_login(sessao, cadastrado)

    auth.trocar_senha(sessao, cadastrado, "senha-nova-456")

    assert auth.usuario_por_token(sessao, token) is None
    assert auth.autenticar(sessao, cadastrado.email, "senha-nova-456") is cadastrado
    with pytest.raises(auth.CredenciaisInvalidas):
        auth.autenticar(sessao, cadastrado.email, "senha-boa-123")


def test_senha_nova_fraca_e_recusada(sessao, cadastrado):
    with pytest.raises(auth.SenhaFraca):
        auth.trocar_senha(sessao, cadastrado, "curta")


# ------------------------------------------------------------ sessão


def test_token_nao_fica_em_texto_puro_no_banco(sessao, cadastrado):
    token = auth.abrir_sessao_de_login(sessao, cadastrado)

    registro = sessao.scalar(select(Session_))
    assert registro.token_hash != token and token not in registro.token_hash


def test_token_valido_encontra_o_usuario(sessao, cadastrado):
    token = auth.abrir_sessao_de_login(sessao, cadastrado)
    assert auth.usuario_por_token(sessao, token) is cadastrado


@pytest.mark.parametrize("ruim", [None, "", "token-inventado"])
def test_token_ruim_nao_encontra_ninguem(sessao, cadastrado, ruim):
    auth.abrir_sessao_de_login(sessao, cadastrado)
    assert auth.usuario_por_token(sessao, ruim) is None


def test_sessao_vencida_nao_vale_e_some(sessao, cadastrado):
    token = auth.abrir_sessao_de_login(sessao, cadastrado)
    sessao.scalar(select(Session_)).expira_em = agora() - timedelta(seconds=1)
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


def test_cadastro_pelo_navegador_ja_entra(cliente_web):
    resposta = cliente_web.post(
        "/cadastro", data={"email": "novo@exemplo.com", "senha": "senha-boa-123"}
    )

    assert resposta.url.path == "/sementes"
    assert 'action="/sair"' in resposta.text  # o botão de sair só aparece logado


def test_cadastro_repetido_avisa_na_tela(cliente_web, cadastrado):
    resposta = cliente_web.post(
        "/cadastro", data={"email": cadastrado.email, "senha": "outra-senha-99"}
    )

    assert "já tem conta" in resposta.text


def test_entrar_e_sair_pelo_navegador(cliente_web, cadastrado):
    resposta = cliente_web.post(
        "/entrar", data={"email": cadastrado.email, "senha": "senha-boa-123"}
    )
    assert resposta.url.path == "/sementes"

    assert cliente_web.post("/sair").url.path == "/"
    assert cliente_web.get("/sementes").url.path == "/entrar"


def test_cookie_de_sessao_e_protegido(cliente_web):
    resposta = cliente_web.post(
        "/cadastro",
        data={"email": "novo@exemplo.com", "senha": "senha-boa-123"},
        follow_redirects=False,
    )

    bruto = resposta.headers["set-cookie"].lower()
    assert "httponly" in bruto        # JavaScript não lê
    assert "samesite=lax" in bruto    # não vaza pra outro site
    assert "secure" not in bruto      # em dev não tem HTTPS; em prod tem


def test_login_errado_nao_diz_qual_dos_dois_errou(cliente_web):
    resposta = cliente_web.post("/entrar", data={"email": "ninguem@x.com", "senha": "chute-errado"})

    assert auth.MENSAGEM_CREDENCIAL_INVALIDA in resposta.text
    assert "não cadastrado" not in resposta.text


def test_nao_expoe_documentacao_de_api(cliente_web):
    """A spec proíbe OpenAPI voltada pra terceiros."""
    for rota in ("/docs", "/redoc", "/openapi.json"):
        assert cliente_web.get(rota).status_code == 404


def test_tela_de_confirmacao_nao_existe_mais(cliente_web):
    assert cliente_web.get("/confirmar").status_code == 404
