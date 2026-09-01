"""Testes da retrospectiva semanal.

A retrospectiva não calcula nada novo: ela mostra o que o feedback já
produziu. O que precisa estar certo é o agrupamento, a taxa e o peso — e o
peso tem que ser exatamente o mesmo que o motor usa pra pontuar.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.garimpo import peso_da_ponte, registrar_feedback
from app.models import User, Veredito
from app.retrospectiva import intervalo_da_semana, pesos_das_pontes, resumo_por_semana


# ------------------------------------------------------------ semana


def test_semana_vira_intervalo_de_segunda_a_domingo():
    segunda, domingo = intervalo_da_semana("2026-W36")

    assert segunda == date(2026, 8, 31)
    assert domingo == date(2026, 9, 6)
    assert segunda.weekday() == 0 and domingo.weekday() == 6


@pytest.mark.parametrize("ruim", ["", "2026", "2026-36", "abc-Wxx", None])
def test_semana_malformada_nao_quebra(ruim):
    assert intervalo_da_semana(ruim) is None


# ------------------------------------------------------------ resumo


def test_sem_descoberta_nenhuma_o_resumo_e_vazio(sessao, cadastrado):
    assert resumo_por_semana(sessao, cadastrado) == []


def test_semanas_vem_da_mais_recente_pra_mais_antiga(sessao, cadastrado, fabricar_descoberta):
    fabricar_descoberta(cadastrado.id, "Velho", semana="2026-W30")
    fabricar_descoberta(cadastrado.id, "Novo", semana="2026-W36")
    fabricar_descoberta(cadastrado.id, "Meio", semana="2026-W33")

    assert [s.semana for s in resumo_por_semana(sessao, cadastrado)] == [
        "2026-W36", "2026-W33", "2026-W30"
    ]


def test_taxa_e_none_quando_nada_foi_julgado(sessao, cadastrado, fabricar_descoberta):
    """Zero de zero não é 0% — é 'não sei ainda'."""
    fabricar_descoberta(cadastrado.id, "Na Fila")

    (semana,) = resumo_por_semana(sessao, cadastrado)
    assert semana.taxa is None
    assert (semana.descobertas, semana.com_veredito, semana.aprovados) == (1, 0, 0)


def test_taxa_de_aprovacao(sessao, cadastrado, fabricar_descoberta):
    a = fabricar_descoberta(cadastrado.id, "Um", 10_000, "Ponte A")
    b = fabricar_descoberta(cadastrado.id, "Dois", 20_000, "Ponte A")
    c = fabricar_descoberta(cadastrado.id, "Tres", 30_000, "Ponte B")
    d = fabricar_descoberta(cadastrado.id, "Quatro", 40_000, "Ponte B")
    registrar_feedback(sessao, a, Veredito.APROVADO)
    registrar_feedback(sessao, b, Veredito.PULADO)
    registrar_feedback(sessao, c, Veredito.BANIDO)
    registrar_feedback(sessao, d, Veredito.APROVADO)

    (semana,) = resumo_por_semana(sessao, cadastrado)
    assert (semana.aprovados, semana.com_veredito) == (2, 4)
    assert semana.taxa == pytest.approx(0.5)


def test_descoberta_na_fila_nao_conta_na_taxa(sessao, cadastrado, fabricar_descoberta):
    a = fabricar_descoberta(cadastrado.id, "Julgado", 10_000)
    fabricar_descoberta(cadastrado.id, "Na Fila", 20_000)
    registrar_feedback(sessao, a, Veredito.APROVADO)

    (semana,) = resumo_por_semana(sessao, cadastrado)
    assert semana.taxa == pytest.approx(1.0)  # 1 de 1 julgado, não 1 de 2
    assert semana.descobertas == 2


def test_media_de_ouvintes_conta_so_os_aprovados(sessao, cadastrado, fabricar_descoberta):
    a = fabricar_descoberta(cadastrado.id, "Aprovado Um", 10_000, "Ponte A")
    b = fabricar_descoberta(cadastrado.id, "Aprovado Dois", 20_000, "Ponte A")
    c = fabricar_descoberta(cadastrado.id, "Banido Enorme", 900_000, "Ponte B")
    registrar_feedback(sessao, a, Veredito.APROVADO)
    registrar_feedback(sessao, b, Veredito.APROVADO)
    registrar_feedback(sessao, c, Veredito.BANIDO)

    (semana,) = resumo_por_semana(sessao, cadastrado)
    assert semana.media_ouvintes == 15_000  # o banido não entra


def test_media_e_none_sem_aprovado_nenhum(sessao, cadastrado, fabricar_descoberta):
    a = fabricar_descoberta(cadastrado.id, "Pulado", 10_000)
    registrar_feedback(sessao, a, Veredito.PULADO)

    (semana,) = resumo_por_semana(sessao, cadastrado)
    assert semana.media_ouvintes is None


def test_ranking_das_pontes_da_semana(sessao, cadastrado, fabricar_descoberta):
    boa1 = fabricar_descoberta(cadastrado.id, "A", 10_000, "Ponte Boa")
    boa2 = fabricar_descoberta(cadastrado.id, "B", 10_000, "Ponte Boa")
    ruim = fabricar_descoberta(cadastrado.id, "C", 10_000, "Ponte Ruim")
    registrar_feedback(sessao, boa1, Veredito.APROVADO)
    registrar_feedback(sessao, boa2, Veredito.APROVADO)
    registrar_feedback(sessao, ruim, Veredito.BANIDO)

    (semana,) = resumo_por_semana(sessao, cadastrado)
    assert [p.nome for p in semana.pontes] == ["Ponte Boa", "Ponte Ruim"]
    assert (semana.pontes[0].aprovados, semana.pontes[0].total) == (2, 2)
    assert semana.pontes[1].taxa == 0.0


def test_cada_semana_tem_o_proprio_ranking(sessao, cadastrado, fabricar_descoberta):
    antiga = fabricar_descoberta(cadastrado.id, "A", 10_000, "Ponte Velha", semana="2026-W30")
    nova = fabricar_descoberta(cadastrado.id, "B", 10_000, "Ponte Nova", semana="2026-W36")
    registrar_feedback(sessao, antiga, Veredito.APROVADO)
    registrar_feedback(sessao, nova, Veredito.APROVADO)

    recente, velha = resumo_por_semana(sessao, cadastrado)
    assert [p.nome for p in recente.pontes] == ["Ponte Nova"]
    assert [p.nome for p in velha.pontes] == ["Ponte Velha"]


def test_retrospectiva_nao_mistura_usuarios(sessao, cadastrado, fabricar_descoberta):
    vizinho = User(email="vizinho@x.com", password_hash="x")
    sessao.add(vizinho)
    sessao.flush()
    alheia = fabricar_descoberta(vizinho.id, "Do Vizinho", 10_000, "Ponte Alheia")
    registrar_feedback(sessao, alheia, Veredito.APROVADO)

    assert resumo_por_semana(sessao, cadastrado) == []
    assert pesos_das_pontes(sessao, cadastrado) == []


# ------------------------------------------------------------ peso das pontes


def test_peso_mostrado_e_o_mesmo_que_o_motor_usa(sessao, cadastrado, fabricar_descoberta):
    """Se estes dois números divergirem, a tela vira mentira."""
    a = fabricar_descoberta(cadastrado.id, "A", 10_000, "Ponte")
    b = fabricar_descoberta(cadastrado.id, "B", 10_000, "Ponte")
    registrar_feedback(sessao, a, Veredito.APROVADO)
    registrar_feedback(sessao, b, Veredito.BANIDO)

    (ponte,) = pesos_das_pontes(sessao, cadastrado)
    assert ponte.peso == peso_da_ponte(ponte.aprovados, ponte.total)
    assert (ponte.aprovados, ponte.total) == (1, 2)


def test_pontes_ordenadas_da_que_mais_rende_pra_que_menos(sessao, cadastrado, fabricar_descoberta):
    boa = fabricar_descoberta(cadastrado.id, "A", 10_000, "Rende")
    ruim = fabricar_descoberta(cadastrado.id, "B", 10_000, "Nao Rende")
    registrar_feedback(sessao, boa, Veredito.APROVADO)
    registrar_feedback(sessao, ruim, Veredito.BANIDO)

    pesos = pesos_das_pontes(sessao, cadastrado)
    assert [p.nome for p in pesos] == ["Rende", "Nao Rende"]
    assert pesos[0].rende and not pesos[1].rende
    assert pesos[0].peso > 1.0 > pesos[1].peso


def test_ponte_sem_historico_nao_aparece(sessao, cadastrado, fabricar_descoberta):
    """Ponte que nunca rendeu veredito nenhum entra neutra no motor; não faz
    sentido ocupar linha na tela."""
    fabricar_descoberta(cadastrado.id, "Na Fila", 10_000, "Ponte Intocada")

    assert pesos_das_pontes(sessao, cadastrado) == []


# ------------------------------------------------------------ tela


def test_retrospectiva_exige_login(cliente_web):
    resposta = cliente_web.get("/retrospectiva", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/entrar"


def test_retrospectiva_vazia(logado):
    assert "nenhuma rodada ainda" in logado.get("/retrospectiva").text


def test_retrospectiva_mostra_os_numeros_da_semana(logado, sessao, cadastrado, fabricar_descoberta):
    a = fabricar_descoberta(cadastrado.id, "Um", 10_000, "Ponte Boa", semana="2026-W36")
    b = fabricar_descoberta(cadastrado.id, "Dois", 30_000, "Ponte Boa", semana="2026-W36")
    c = fabricar_descoberta(cadastrado.id, "Tres", 50_000, "Ponte Ruim", semana="2026-W36")
    registrar_feedback(sessao, a, Veredito.APROVADO)
    registrar_feedback(sessao, b, Veredito.APROVADO)
    registrar_feedback(sessao, c, Veredito.BANIDO)

    corpo = logado.get("/retrospectiva").text

    assert "2026-W36" in corpo
    assert "31/08 a 06/09/2026" in corpo
    assert ">2</div>" in corpo          # aprovados
    assert ">67%</div>" in corpo        # 2 de 3
    assert "20.000" in corpo            # média dos aprovados
    assert "Ponte Boa" in corpo


def test_tela_mostra_o_peso_atual_das_pontes(logado, sessao, cadastrado, fabricar_descoberta):
    a = fabricar_descoberta(cadastrado.id, "Um", 10_000, "Ponte Boa")
    registrar_feedback(sessao, a, Veredito.APROVADO)

    corpo = logado.get("/retrospectiva").text

    assert "peso das pontes agora" in corpo
    assert "1.33" in corpo  # (1+1)/(1+2) normalizado


def test_menu_leva_a_retrospectiva(logado):
    assert 'href="/retrospectiva"' in logado.get("/historico").text


def test_barra_de_peso_precisa_ser_bloco(cliente_web):
    """Em `<span>` inline o navegador ignora width e height, e a barra some —
    sobra só o trilho vazio, igual pra todas as pontes."""
    import re as _re

    css = cliente_web.get("/static/estilo.css").text
    regra = _re.search(r"\.peso-barra\s*\{[^}]*\}", css)
    assert regra and "display: block" in regra.group(0)
