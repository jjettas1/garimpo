"""Testes do motor.

A parte de cima cobre a matemática pura (score, combinação de rotas, peso da
ponte). A de baixo roda o garimpo inteiro contra o banco em memória, com o
cliente falso.
"""

from __future__ import annotations

import math

import pytest

from app.garimpo import (
    PISO_COERENCIA,
    TETO_PESO_ROTA,
    Candidato,
    Rota,
    calcular_score,
    coerencia,
    combinar_afinidade,
    garimpar,
    peso_da_ponte,
    peso_da_rota,
    raridade,
    registrar_feedback,
    semana_de,
)
from app.lastfm import CacheArtistas
from app.models import BridgeStat, Feedback, Veredito

# ------------------------------------------------------------ peso da ponte


def test_ponte_sem_historico_e_neutra():
    assert peso_da_ponte(aprovados=0, total=0) == pytest.approx(1.0)


def test_ponte_que_rende_pesa_mais_que_ponte_que_erra():
    boa = peso_da_ponte(aprovados=9, total=10)
    ruim = peso_da_ponte(aprovados=1, total=10)
    assert boa > 1.0 > ruim
    assert boa < 2.0 and ruim > 0.0  # Laplace nunca deixa cravar 0 nem 2


def test_laplace_segura_o_julgamento_com_pouca_amostra():
    """Um acerto em um só não pode valer o mesmo que nove em dez."""
    assert peso_da_ponte(1, 1) < peso_da_ponte(9, 10)


# ------------------------------------------------------------ peso da rota


def test_peso_da_rota_multiplica_matches_e_aplica_decaimento():
    peso = peso_da_rota([0.9, 0.5], peso_ponte=1.0, decaimento=0.8)
    assert peso == pytest.approx(0.9 * 0.5 * 0.8)


def test_um_salto_so_nao_sofre_decaimento():
    assert peso_da_rota([0.6], peso_ponte=1.0, decaimento=0.8) == pytest.approx(0.6)


def test_ponte_boa_puxa_a_rota_pra_cima():
    matches = [0.9, 0.9]
    assert peso_da_rota(matches, 1.5, 0.85) > peso_da_rota(matches, 1.0, 0.85)


def test_peso_da_rota_nunca_chega_a_um():
    assert peso_da_rota([1.0, 1.0], peso_ponte=2.0, decaimento=1.0) == TETO_PESO_ROTA


# ------------------------------------------------------ combinação de rotas


def test_uma_rota_so_devolve_o_proprio_peso():
    assert combinar_afinidade([0.4]) == pytest.approx(0.4)


def test_varias_rotas_fracas_somam_mais_que_cada_uma():
    pesos = [0.3, 0.3, 0.3]
    combinada = combinar_afinidade(pesos)
    assert combinada > max(pesos)
    assert combinada == pytest.approx(1 - 0.7**3)


def test_combinacao_nunca_satura_em_um():
    """Sem o teto, vinte rotas fortes dariam 1.0 no float e empatariam todo
    mundo — o ranking perderia a graça."""
    assert combinar_afinidade([0.99] * 20) == TETO_PESO_ROTA


def test_sem_rota_nenhuma_a_afinidade_e_zero():
    assert combinar_afinidade([]) == 0.0


# ------------------------------------------------------------ raridade


def test_raridade_e_maxima_no_piso_e_minima_no_teto():
    assert raridade(1_000, 1_000, 200_000) == pytest.approx(1.0)
    assert raridade(200_000, 1_000, 200_000) == pytest.approx(0.0)


def test_raridade_cai_conforme_ganha_ouvintes():
    assert raridade(2_000, 1_000, 200_000) > raridade(50_000, 1_000, 200_000)


def test_raridade_e_logaritmica_nao_linear():
    """No meio geométrico da faixa a raridade dá 0.5 — no meio aritmético, não."""
    meio_geometrico = math.sqrt(1_000 * 200_000)
    assert raridade(int(meio_geometrico), 1_000, 200_000) == pytest.approx(0.5, abs=1e-3)
    assert raridade(100_500, 1_000, 200_000) < 0.2


def test_raridade_grampeia_fora_da_faixa():
    assert raridade(10, 1_000, 200_000) == pytest.approx(1.0)
    assert raridade(9_000_000, 1_000, 200_000) == pytest.approx(0.0)


# ------------------------------------------------------------ coerência


def test_tags_iguais_dao_coerencia_cheia():
    assert coerencia(["slowcore", "rock"], {"slowcore", "rock", "indie"}) == pytest.approx(1.0)


def test_tags_sem_encontro_caem_no_piso():
    assert coerencia(["sertanejo"], {"slowcore", "rock"}) == PISO_COERENCIA


def test_artista_sem_tag_nao_e_zerado():
    assert coerencia([], {"rock"}) == PISO_COERENCIA


def test_coerencia_ignora_acento_e_caixa():
    assert coerencia(["Post-Rock"], {"post-rock"}) == pytest.approx(1.0)


# ------------------------------------------------------------ score


def test_score_e_o_produto_das_tres_partes():
    esperado = 0.5 * (0.8**0.7) * (0.6**0.5)
    assert calcular_score(0.5, 0.8, 0.6) == pytest.approx(esperado)


def test_afinidade_manda_mais_que_raridade():
    """Os expoentes < 1 achatam raridade e coerência de propósito."""
    dobrou_afinidade = calcular_score(0.8, 0.4, 0.5)
    dobrou_raridade = calcular_score(0.4, 0.8, 0.5)
    assert dobrou_afinidade > dobrou_raridade


def test_afinidade_zerada_zera_o_score():
    assert calcular_score(0.0, 1.0, 1.0) == 0.0


def test_semana_no_formato_da_retrospectiva():
    from datetime import datetime, timezone

    assert semana_de(datetime(2026, 8, 20, tzinfo=timezone.utc)) == "2026-W34"


# ------------------------------------------------------------ candidato


def test_candidato_escolhe_a_rota_mais_forte():
    fraca = Rota("S", "A", "X", [], 0.2)
    forte = Rota("S", "B", "X", [], 0.6)
    candidato = Candidato(nome="X", rotas=[fraca, forte])
    assert candidato.melhor_rota is forte
    assert candidato.afinidade == pytest.approx(1 - 0.8 * 0.4)


# ------------------------------------------------------ garimpo de ponta a ponta


def test_garimpo_acha_o_alvo_e_descarta_fora_da_faixa(sessao, usuario, cliente, config):
    descobertas = garimpar(sessao, usuario, cliente=cliente, config=config)

    nomes = [d.artista.nome for d in descobertas]
    assert nomes == ["Alvo"]  # "Famoso" passa do teto, "Raro" não chega no piso


def test_garimpo_guarda_as_duas_rotas_que_chegaram_no_alvo(sessao, usuario, cliente, config):
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)

    assert descoberta.n_rotas == 2
    pontes = {rota["ponte"] for rota in descoberta.rota}
    assert pontes == {"Ponte A", "Ponte B"}


def test_rota_gravada_desenha_o_caminho_inteiro(sessao, usuario, cliente, config):
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)

    melhor = descoberta.rota[0]
    assert melhor["semente"] == "Semente"
    assert [p["papel"] for p in melhor["passos"]] == ["semente->ponte", "ponte->candidato"]
    assert melhor["passos"][0]["de"] == "Semente"
    assert melhor["passos"][-1]["para"] == "Alvo"


def test_afinidade_gravada_e_a_combinacao_das_rotas(sessao, usuario, cliente, config):
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)

    d = config.decaimento_salto
    por_a = 0.9 * 0.7 * d
    por_b = 0.8 * 0.6 * d
    assert descoberta.afinidade == pytest.approx(1 - (1 - por_a) * (1 - por_b))


def test_rodada_fica_marcada_como_pronta(sessao, usuario, cliente, config):
    from app.models import Run, StatusRun

    garimpar(sessao, usuario, cliente=cliente, config=config)
    rodada = sessao.query(Run).one()
    assert rodada.status == StatusRun.PRONTO
    assert rodada.terminado_em is not None


def test_cache_evita_bater_de_novo_na_api(sessao, usuario, cliente, config):
    garimpar(sessao, usuario, cliente=cliente, config=config)
    chamadas_da_primeira = len(cliente.chamadas_info)

    cache = CacheArtistas(sessao, cliente)
    cache.obter("Alvo")
    assert len(cliente.chamadas_info) == chamadas_da_primeira  # veio do banco


def test_aprovar_engorda_o_placar_da_ponte(sessao, usuario, cliente, config):
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)

    registrar_feedback(sessao, descoberta, Veredito.APROVADO)

    placar = sessao.query(BridgeStat).one()
    assert (placar.aprovados, placar.total) == (1, 1)
    assert peso_da_ponte(placar.aprovados, placar.total) > 1.0


def test_banir_conta_no_total_mas_nao_nos_aprovados(sessao, usuario, cliente, config):
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)

    registrar_feedback(sessao, descoberta, Veredito.BANIDO)

    placar = sessao.query(BridgeStat).one()
    assert (placar.aprovados, placar.total) == (0, 1)
    assert peso_da_ponte(placar.aprovados, placar.total) < 1.0


def test_artista_banido_nao_volta_na_proxima_rodada(sessao, usuario, cliente, config):
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)
    registrar_feedback(sessao, descoberta, Veredito.BANIDO)

    de_novo = garimpar(sessao, usuario, cliente=cliente, config=config)
    assert de_novo == []


# ------------------------------------------------------ trocar de veredito

# `bridge_stats` é materializado: ninguém recalcula depois. Um erro de
# contagem aqui estraga o peso da ponte pra sempre.


def test_repetir_o_mesmo_veredito_nao_conta_de_novo(sessao, usuario, cliente, config):
    """Clique duplo, ou refresh depois do POST. Não pode contar duas vezes."""
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)

    registrar_feedback(sessao, descoberta, Veredito.APROVADO)
    registrar_feedback(sessao, descoberta, Veredito.APROVADO)

    placar = sessao.query(BridgeStat).one()
    assert (placar.aprovados, placar.total) == (1, 1)


def test_trocar_de_ideia_move_o_placar_em_vez_de_somar(sessao, usuario, cliente, config):
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)

    registrar_feedback(sessao, descoberta, Veredito.APROVADO)
    registrar_feedback(sessao, descoberta, Veredito.BANIDO)

    placar = sessao.query(BridgeStat).one()
    assert (placar.aprovados, placar.total) == (0, 1)  # o total não inflou
    assert peso_da_ponte(placar.aprovados, placar.total) < 1.0


def test_mudar_de_pulado_para_aprovado(sessao, usuario, cliente, config):
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)

    registrar_feedback(sessao, descoberta, Veredito.PULADO)
    registrar_feedback(sessao, descoberta, Veredito.APROVADO)

    placar = sessao.query(BridgeStat).one()
    assert (placar.aprovados, placar.total) == (1, 1)


def test_o_veredito_final_e_o_que_vale(sessao, usuario, cliente, config):
    (descoberta,) = garimpar(sessao, usuario, cliente=cliente, config=config)

    registrar_feedback(sessao, descoberta, Veredito.APROVADO)
    registrar_feedback(sessao, descoberta, Veredito.PULADO)

    assert descoberta.feedback.veredito == Veredito.PULADO
    assert len(sessao.query(Feedback).all()) == 1  # não duplicou a linha


# ------------------------------------------------------ limite do last.fm


def test_limitador_e_do_processo_e_nao_da_instancia(monkeypatch):
    """O teto de 5 req/s é do Last.fm, não de cada objeto.

    Com o contador por instância, um autocomplete rodando junto com um
    garimpo davam dois orçamentos de 4/s e estouravam o limite deles.
    """
    from app import lastfm

    dormidas: list[float] = []
    monkeypatch.setattr(lastfm.time, "sleep", dormidas.append)
    lastfm.zerar_limitador()

    autocomplete = lastfm.Limitador(4)
    garimpo_de_fundo = lastfm.Limitador(4)  # outro objeto, outro "cliente"

    autocomplete.esperar()      # primeira do processo: passa direto
    garimpo_de_fundo.esperar()  # tem que respeitar a vez do outro

    assert dormidas, "o segundo cliente ignorou o limite do primeiro"
    assert dormidas[0] == pytest.approx(0.25, abs=0.06)


def test_sem_limite_configurado_nao_segura_ninguem(monkeypatch):
    from app import lastfm

    dormidas: list[float] = []
    monkeypatch.setattr(lastfm.time, "sleep", dormidas.append)
    lastfm.zerar_limitador()

    solto = lastfm.Limitador(0)
    solto.esperar()
    solto.esperar()

    assert dormidas == []
