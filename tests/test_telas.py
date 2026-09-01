"""Testes das telas: sementes, garimpo e a identidade visual.

O que importa aqui não é o pixel — é que a rota apareça inteira, que a chave
do Last.fm não vaze pro HTML e que aprovar não recarregue a página.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.models import Discovery, Feedback, Run, SeedArtist, StatusRun, User, Veredito


# ------------------------------------------------------------ porta trancada


@pytest.mark.parametrize("rota", ["/sementes", "/garimpo", "/garimpo/estado"])
def test_telas_internas_exigem_login(cliente_web, rota):
    resposta = cliente_web.get(rota, follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/entrar"


# ------------------------------------------------------------ sementes


def test_tela_de_sementes_abre_vazia(logado):
    corpo = logado.get("/sementes").text
    assert "nenhuma semente ainda" in corpo
    assert "faltam 3 pra poder garimpar" in corpo


def test_adicionar_semente(logado, sessao, cadastrado):
    corpo = logado.post("/sementes/adicionar", data={"nome": "Duster"}).text

    assert "Duster" in corpo
    assert sessao.scalars(select(SeedArtist)).all()[0].nome == "Duster"


def test_semente_repetida_nao_entra_duas_vezes(logado, sessao):
    logado.post("/sementes/adicionar", data={"nome": "Duster"})
    logado.post("/sementes/adicionar", data={"nome": "duster"})

    assert len(sessao.scalars(select(SeedArtist)).all()) == 1


def test_limite_de_oito_sementes(logado, sessao):
    for n in range(10):
        logado.post("/sementes/adicionar", data={"nome": f"Artista {n}"})

    assert len(sessao.scalars(select(SeedArtist)).all()) == 8


def test_remover_semente(logado, sessao):
    logado.post("/sementes/adicionar", data={"nome": "Duster"})
    semente = sessao.scalars(select(SeedArtist)).one()

    corpo = logado.delete(f"/sementes/{semente.id}").text

    assert "nenhuma semente ainda" in corpo
    assert sessao.scalars(select(SeedArtist)).all() == []


def test_ninguem_remove_semente_de_outro(logado, sessao, cadastrado, config):
    from app import auth

    outro = auth.cadastrar
    vizinho = User(email="vizinho@x.com", password_hash="x", email_verificado=True)
    sessao.add(vizinho)
    sessao.flush()
    alheia = SeedArtist(user_id=vizinho.id, nome="Nao Mexa", ordem=0)
    sessao.add(alheia)
    sessao.flush()

    logado.delete(f"/sementes/{alheia.id}")

    assert sessao.get(SeedArtist, alheia.id) is not None


def test_autocomplete_busca_no_lastfm(logado):
    corpo = logado.get("/sementes/buscar", params={"termo": "Sement"}).text
    assert "Semente" in corpo


def test_autocomplete_ignora_termo_curto(logado, cliente):
    logado.get("/sementes/buscar", params={"termo": "a"})
    assert cliente.chamadas_similares == []  # nem chegou a bater na API


def test_autocomplete_nao_sugere_o_que_ja_e_semente(com_sementes):
    corpo = com_sementes.get("/sementes/buscar", params={"termo": "Semente"}).text
    assert "nada encontrado" in corpo


def test_salvar_faixa_de_ouvintes(logado, sessao, cadastrado):
    corpo = logado.post("/sementes/faixa", data={"piso": "500", "teto": "50000"}).text

    assert "faixa salva" in corpo
    assert (cadastrado.piso_ouvintes, cadastrado.teto_ouvintes) == (500, 50_000)


def test_faixa_invertida_e_recusada(logado, sessao, cadastrado):
    antes = cadastrado.piso_ouvintes
    corpo = logado.post("/sementes/faixa", data={"piso": "90000", "teto": "1000"}).text

    assert "o piso precisa ser menor que o teto" in corpo
    assert cadastrado.piso_ouvintes == antes


# ------------------------------------------------------------ garimpo


def test_garimpo_pede_sementes_antes_de_tudo(logado):
    corpo = logado.get("/garimpo/estado").text
    assert "precisa de pelo menos 3 sementes" in corpo


def test_garimpar_responde_na_hora_e_roda_por_tras(com_sementes, sessao):
    """A resposta do POST sai antes da rodada terminar — é o ponto de não
    rodar o garimpo dentro do request. Quem traz o resultado é o polling."""
    corpo = com_sementes.post("/garimpo").text

    assert "garimpando" in corpo
    assert 'hx-trigger="every 2s"' in corpo

    rodada = sessao.scalars(select(Run)).one()  # uma rodada só, não duas
    assert rodada.status == StatusRun.PRONTO
    assert "Alvo" in com_sementes.get("/garimpo/estado").text


def test_rodada_nao_fica_rodando_pra_sempre(com_sementes, sessao):
    """O bug que existiria se a rota e o motor abrissem cada um a sua rodada:
    a primeira ficaria em `rodando` e o polling nunca pararia."""
    com_sementes.post("/garimpo")

    assert sessao.scalars(select(Run).where(Run.status == StatusRun.RODANDO)).all() == []
    assert "hx-trigger=\"every 2s\"" not in com_sementes.get("/garimpo/estado").text


def test_card_mostra_a_rota_inteira_com_as_setas(com_sementes):
    com_sementes.post("/garimpo")
    corpo = com_sementes.get("/garimpo/estado").text

    assert "Semente" in corpo and "Ponte A" in corpo and "Alvo" in corpo
    assert "&#39;" not in corpo  # a rota não pode sair como dicionário cru
    assert re.search(r"──\d\.\d\d──&gt;", corpo), "as setas com o match sumiram"


def test_card_mostra_ouvintes_tags_e_medidas(com_sementes):
    com_sementes.post("/garimpo")
    corpo = com_sementes.get("/garimpo/estado").text

    assert "5.000 ouvintes" in corpo
    assert "slowcore" in corpo
    assert "afinidade" in corpo and "raridade" in corpo and "coerência" in corpo


def test_card_avisa_quando_chegou_por_varios_caminhos(com_sementes):
    com_sementes.post("/garimpo")
    corpo = com_sementes.get("/garimpo/estado").text

    assert "chegou por mais 1 caminho" in corpo


def test_os_tres_botoes_estao_no_card(com_sementes):
    com_sementes.post("/garimpo")
    corpo = com_sementes.get("/garimpo/estado").text

    for rotulo in ("aprovar", "pular", "nunca mais"):
        assert f">{rotulo}</button>" in corpo


def test_aprovar_grava_veredito_e_devolve_so_o_palco(com_sementes, sessao):
    com_sementes.post("/garimpo")
    descoberta = sessao.scalars(select(Discovery)).first()

    resposta = com_sementes.post(f"/garimpo/{descoberta.id}/aprovado")

    assert sessao.scalars(select(Feedback)).one().veredito == Veredito.APROVADO
    # é um pedaço, não uma página: não vem <html>
    assert "<html" not in resposta.text
    assert "nada na fila" in resposta.text


def test_banir_tira_o_artista_da_fila(com_sementes, sessao):
    com_sementes.post("/garimpo")
    descoberta = sessao.scalars(select(Discovery)).first()

    com_sementes.post(f"/garimpo/{descoberta.id}/banido")

    assert sessao.scalars(select(Feedback)).one().veredito == Veredito.BANIDO
    assert "Alvo" not in com_sementes.get("/garimpo/estado").text


def test_veredito_inventado_nao_quebra_a_tela(com_sementes, sessao):
    com_sementes.post("/garimpo")
    descoberta = sessao.scalars(select(Discovery)).first()

    resposta = com_sementes.post(f"/garimpo/{descoberta.id}/sei-la")

    assert resposta.status_code == 200
    assert sessao.scalars(select(Feedback)).all() == []


def test_ninguem_da_veredito_na_descoberta_de_outro(com_sementes, sessao, cadastrado):
    com_sementes.post("/garimpo")
    descoberta = sessao.scalars(select(Discovery)).first()
    descoberta.user_id = 9999  # finge que é de outra pessoa
    sessao.flush()

    com_sementes.post(f"/garimpo/{descoberta.id}/aprovado")

    assert sessao.scalars(select(Feedback)).all() == []


# ------------------------------------------------------------ identidade visual


def test_a_chave_do_lastfm_nunca_aparece_no_html(com_sementes, config):
    """A spec é explícita: nem no HTML, nem no JavaScript."""
    com_sementes.post("/garimpo")

    for rota in ("/", "/sementes", "/garimpo", "/garimpo/estado"):
        assert config.lastfm_api_key not in com_sementes.get(rota).text


def test_css_e_servido_com_as_cores_da_spec(cliente_web):
    css = cliente_web.get("/static/estilo.css").text

    for cor in ("#080b09", "#0b0f0c", "#00e05f", "#4f8f69", "#2f5c45", "#1d3527"):
        assert cor in css, f"a cor {cor} sumiu da folha de estilo"


def test_css_nao_tem_gradiente_glow_nem_sombra(cliente_web):
    css = cliente_web.get("/static/estilo.css").text.lower()
    # os comentários saem antes: eles falam *sobre* gradiente, em português
    sem_comentarios = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    for proibido in ("gradient", "box-shadow", "text-shadow", "drop-shadow"):
        assert proibido not in sem_comentarios, f"a spec proíbe {proibido}"


def test_fonte_e_htmx_sao_self_hosted(cliente_web):
    """Sem Google Fonts e sem CDN: tudo sai do nosso /static."""
    assert cliente_web.get("/static/JetBrainsMono-Regular.woff2").status_code == 200
    assert cliente_web.get("/static/htmx.min.js").status_code == 200

    corpo = cliente_web.get("/").text
    assert "fonts.googleapis.com" not in corpo
    assert "unpkg.com" not in corpo and "cdn." not in corpo


def test_landing_e_publica_e_mostra_uma_rota(cliente_web):
    corpo = cliente_web.get("/").text

    assert "Codeine" in corpo and "C-Clamp" in corpo
    assert re.search(r"──\d\.\d\d──&gt;", corpo)


def test_tela_de_codigo_tem_seis_campos_de_um_digito(cliente_web, correio):
    cliente_web.post("/cadastro", data={"email": "novo@x.com", "senha": "senha-boa-123"})
    corpo = cliente_web.get("/confirmar").text

    assert corpo.count('maxlength="1"') == 6
    assert corpo.count('name="codigo"') == 6


def test_seis_campos_chegam_juntos_no_servidor(cliente_web, correio):
    """Cada campo manda um dígito; o servidor junta. Funciona sem javascript."""
    cliente_web.post("/cadastro", data={"email": "novo@x.com", "senha": "senha-boa-123"})
    digitos = list(correio.ultimo_codigo)

    resposta = cliente_web.post("/confirmar", data={"codigo": digitos})

    assert resposta.url.path == "/sementes"


# ------------------------------------------------------------ rodada que falha

# Os três testes abaixo cobrem bugs que a suíte não pegava porque, em teste,
# as duas sessões dividem a mesma conexão. Só apareceram no servidor real.


def test_rodada_que_falha_nao_deixa_a_tela_travada(com_sementes, sessao, cliente, monkeypatch):
    """O pior modo de falha: a rodada morre, ninguém marca a linha em `runs`,
    e a tela fica em 'garimpando…' pra sempre."""
    def explodir(*_, **__):
        raise RuntimeError("o last.fm caiu")

    monkeypatch.setattr(cliente, "similares", explodir)
    com_sementes.post("/garimpo")

    rodada = sessao.scalars(select(Run)).one()
    assert rodada.status == StatusRun.ERRO
    assert rodada.terminado_em is not None

    corpo = com_sementes.get("/garimpo/estado").text
    assert 'hx-trigger="every 2s"' not in corpo  # o polling parou
    assert "tentar de novo" in corpo


def test_erro_da_rodada_nao_vaza_a_chave_do_lastfm(com_sementes, sessao, cliente, config, monkeypatch):
    """O httpx põe a URL inteira na exceção — e a URL tem `api_key=...`.
    Essa mensagem vai pro banco e aparece na tela."""
    def explodir(*_, **__):
        raise RuntimeError(
            f"403 Forbidden for url 'https://ws.audioscrobbler.com/2.0/"
            f"?api_key={config.lastfm_api_key}&method=artist.getInfo'"
        )

    monkeypatch.setattr(cliente, "similares", explodir)
    com_sementes.post("/garimpo")

    rodada = sessao.scalars(select(Run)).one()
    assert config.lastfm_api_key not in rodada.erro
    assert "***" in rodada.erro
    assert config.lastfm_api_key not in com_sementes.get("/garimpo/estado").text


def test_engine_de_arquivo_liga_o_wal(tmp_path):
    """Sem WAL, o garimpo escrevendo em segundo plano trava o polling lendo:
    `database is locked` na primeira rodada."""
    from sqlalchemy import text

    from app.db import criar_engine

    engine = criar_engine(f"sqlite:///{tmp_path / 'teste.db'}")
    with engine.connect() as conexao:
        assert conexao.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conexao.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_leitura_nao_trava_enquanto_outro_escreve(tmp_path):
    """O caso concreto: a tarefa de fundo com uma escrita aberta, o polling
    querendo ler. Em modo journal padrão isso estoura."""
    from sqlalchemy.orm import sessionmaker

    from app.db import criar_engine
    from app.models import Base

    engine = criar_engine(f"sqlite:///{tmp_path / 'teste.db'}")
    Base.metadata.create_all(engine)
    Fabrica = sessionmaker(bind=engine)

    with Fabrica() as escritor, Fabrica() as leitor:
        escritor.add(User(email="a@b.com", password_hash="x"))
        escritor.flush()  # escrita aberta, ainda sem commit

        assert leitor.scalars(select(User)).all() == []  # lê sem travar

        escritor.commit()
        assert len(leitor.scalars(select(User)).all()) == 1


# ------------------------------------------------------------ histórico


def test_historico_exige_login(cliente_web, fabricar_descoberta):
    resposta = cliente_web.get("/historico", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/entrar"


def test_historico_comeca_vazio(logado, fabricar_descoberta):
    corpo = logado.get("/historico").text
    assert "nada aprovado ainda" in corpo


def test_historico_mostra_so_o_que_foi_aprovado(logado, sessao, cadastrado, fabricar_descoberta):
    from app.garimpo import registrar_feedback

    aprovado = fabricar_descoberta(cadastrado.id, "Aprovado", 12_000, "Ponte Um")
    pulado = fabricar_descoberta(cadastrado.id, "Pulado", 8_000, "Ponte Dois")
    banido = fabricar_descoberta(cadastrado.id, "Banido", 4_000, "Ponte Tres")
    registrar_feedback(sessao, aprovado, Veredito.APROVADO)
    registrar_feedback(sessao, pulado, Veredito.PULADO)
    registrar_feedback(sessao, banido, Veredito.BANIDO)

    corpo = logado.get("/historico").text

    assert "Aprovado" in corpo
    assert "Pulado" not in corpo and "Banido" not in corpo
    assert "1 artista aprovado" in corpo


def test_historico_sem_veredito_nao_aparece(logado, sessao, cadastrado, fabricar_descoberta):
    fabricar_descoberta(cadastrado.id, "Ainda Na Fila", 9_000)

    assert "Ainda Na Fila" not in logado.get("/historico").text


def test_historico_preserva_a_rota_inteira(logado, sessao, cadastrado, fabricar_descoberta):
    """O ponto do histórico: daqui a seis meses ainda dá pra saber por que
    aquele nome apareceu."""
    from app.garimpo import registrar_feedback

    d = fabricar_descoberta(cadastrado.id, "Guardado", 15_000, "Bedhead")
    registrar_feedback(sessao, d, Veredito.APROVADO)

    corpo = logado.get("/historico").text

    assert "Codeine" in corpo and "Bedhead" in corpo and "Guardado" in corpo
    assert re.search(r"──\d\.\d\d──&gt;", corpo), "a rota perdeu as setas"
    assert "ponte:" in corpo and "15.000 ouvintes" in corpo


def test_historico_vem_do_mais_recente_pro_mais_antigo(logado, sessao, cadastrado, fabricar_descoberta):
    from datetime import timedelta

    from app.garimpo import registrar_feedback
    from app.models import agora

    antigo = fabricar_descoberta(cadastrado.id, "Antigo", 10_000, "Ponte A")
    recente = fabricar_descoberta(cadastrado.id, "Recente", 20_000, "Ponte B")
    registrar_feedback(sessao, antigo, Veredito.APROVADO)
    registrar_feedback(sessao, recente, Veredito.APROVADO)
    antigo.feedback.criado_em = agora() - timedelta(days=30)
    sessao.flush()

    corpo = logado.get("/historico").text

    assert corpo.index("Recente") < corpo.index("Antigo")


def test_ninguem_ve_o_historico_de_outro(logado, sessao, cadastrado, fabricar_descoberta):
    from app.garimpo import registrar_feedback

    vizinho = User(email="vizinho@x.com", password_hash="x", email_verificado=True)
    sessao.add(vizinho)
    sessao.flush()
    alheia = fabricar_descoberta(vizinho.id, "Segredo Do Vizinho", 7_000)
    registrar_feedback(sessao, alheia, Veredito.APROVADO)

    assert "Segredo Do Vizinho" not in logado.get("/historico").text


def test_data_do_aprovado_sai_no_fuso_de_quem_olha(logado, sessao, cadastrado, fabricar_descoberta):
    """O banco guarda UTC naive; a tela converte. É o único lugar do projeto
    onde fuso horário existe."""
    from app.garimpo import registrar_feedback
    from app.web import _data_local

    d = fabricar_descoberta(cadastrado.id, "Datado", 11_000)
    registrar_feedback(sessao, d, Veredito.APROVADO)

    assert _data_local(d.feedback.criado_em) in logado.get("/historico").text
    assert _data_local(None) == ""


def test_menu_leva_ao_historico(logado):
    assert 'href="/historico"' in logado.get("/garimpo").text
