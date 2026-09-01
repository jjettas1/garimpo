# Garimpo — instruções do projeto

Descoberta de artistas underground a partir do que a pessoa já escuta,
mostrando **a rota** que ligou um ao outro. A rota é o produto, não um detalhe.

A especificação completa está em `PROMPT-CLAUDE-CODE.md`. Este arquivo é o
resumo operacional.

## Stack (decidida, não sugerir alternativa)

- Python 3.11+, FastAPI
- SQLite via SQLAlchemy 2.x (síncrono)
- Jinja2 no servidor + HTMX. Sem React, sem SPA, sem build de frontend
- Alembic pra migrations
- Sem Docker, sem Celery/RQ, sem Redis
- Deploy no Render (`render.yaml`). **Não** serverless: o app precisa de
  disco persistente, processo de pé e memória entre requisições
- Last.fm é a **única** fonte externa de dados musicais. Nada de Spotify

## Estrutura

```
app/          todo o código Python (módulos achatados, sem subpacotes)
migrations/   alembic
tests/        pytest
```

Os módulos ficam achatados de propósito (`models.py`, `auth.py`, `web.py`…)
em vez dos subpacotes `auth/ discovery/ web/ models/` da spec — decisão de
organização pra manter o projeto com poucas pastas.

## Convenções

- Código em português: variáveis, funções, comentários
- Funções pequenas, com type hints
- Segredos só por variável de ambiente; `.env.example` versionado,
  `.env` nunca commitado
- Testes com pytest, sem bater na API real do Last.fm (usar fixtures)
- Commits pequenos, em português, Conventional Commits

## Identidade visual (etapa 3 em diante)

Terminal escuro, sério — não é paródia.

| papel | cor |
|---|---|
| fundo | `#080b09` |
| superfície | `#0b0f0c` |
| ação/destaque | `#00e05f` |
| texto secundário | `#4f8f69` |
| texto terciário | `#2f5c45` |
| borda hairline | `#1d3527` |

Fonte monoespaçada self-hosted (JetBrains Mono em `app/static/`, com a
OFL junto), rótulos em caixa baixa, tudo em `app/static/estilo.css` com as
cores em variáveis no `:root`. Sem gradiente, glow, sombra, animação
decorativa ou ASCII art — a única animação é o ponto pulsante que indica
rodada em andamento, e ela respeita `prefers-reduced-motion`.

Duas cores não estavam na spec e foram derivadas: `--texto` (#dbe7de, o
branco puxado pro verde) e `--perigo` (#c0503c, vermelho dessaturado).

O HTMX também é self-hosted (`app/static/htmx.min.js`). Nada de CDN.

## Regras da autenticação

> **Desvio consciente da spec.** O `PROMPT-CLAUDE-CODE.md` pede verificação
> por código de 6 dígitos no email. Isso foi **removido** a pedido: cadastrar
> entra direto. A migration `0002` derrubou `verification_codes`,
> `envios_codigo` e `users.email_verificado`.

- Senha: **argon2id**, nunca sha256 nem bcrypt
- Token de sessão: 32 bytes aleatórios, guardado em **sha256** — e isso está
  certo. Com 256 bits de entropia não há dicionário a temer, e o sal
  aleatório do argon2 impediria procurar a sessão pelo hash
- Datas: **UTC naive** no banco e no código. O SQLite descarta o fuso em
  silêncio, então misturar aware e naive estoura em comparação
- **O login não pode revelar se um email tem conta**: mensagem única byte a
  byte, e hash dummy pra igualar o tempo quando a conta não existe
- **O cadastro revela**, e não tem como não revelar sem verificação por
  email — ele precisa dizer se criou a conta ou não. Se um dia o código por
  email voltar, essa proteção volta junto
- Não há rate limit em lugar nenhum. O custo do argon2 (~100ms) é o que
  segura força bruta no login; se precisar de mais, uma tabela de tentativas
  resolve, do mesmo jeito que o limite de envio de código fazia antes

## Regras do feedback

`bridge_stats` é **materializado**: ninguém recalcula a partir do `feedback`
depois. Então toda mudança de veredito tem que mover o placar, nunca somar de
novo — repetir o mesmo veredito não faz nada, e trocar de ideia desconta o
anterior antes de somar o novo.

## Regras do Last.fm

O limitador de 4 req/s vive em **variável de módulo**, não de instância: o
teto é da API, não de cada objeto. Vale por processo — com mais de um worker
do uvicorn, o teto precisaria sair pra um lugar compartilhado.

## Regras do SQLite

O garimpo escreve numa tarefa de fundo enquanto a tela faz polling — dois
escritores no mesmo arquivo. O `criar_engine()` liga **WAL** e um timeout de
30s por isso; sem eles a primeira rodada morre com `database is locked`.

Regra que decorre disso: **quem abre uma linha que a tarefa de fundo vai
usar precisa commitar antes de agendar a tarefa.** A tarefa abre outra
sessão e não enxerga o que ainda está pendente.

## Regras do motor

- `score = afinidade × raridade^0.7 × coerência^0.5`
- `afinidade = 1 - Π(1 - wᵢ)` quando o artista chega por várias rotas
- peso da ponte = `(aprovados + 1) / (total + 2)`, normalizado pra 1.0 no neutro
- cache de artista em `artists`; não bater na API se `atualizado_em` < 14 dias
- máximo 4 req/s no Last.fm (limite deles é 5)
- garimpo **não roda dentro do request** — `BackgroundTasks` + tabela `runs`
  + polling via HTMX

## O que não fazer

- Login social (Google, Spotify, GitHub)
- Machine learning no scoring — é determinístico e explicável de propósito
- Framework CSS ou biblioteca de componentes
- Dados de exemplo falsos no banco de produção
- API pública / OpenAPI pra terceiros
- Expor a chave do Last.fm no HTML ou em JavaScript

## Ordem de construção

1. Estrutura, config, migrations, motor por linha de comando ← **feito**
2. Auth completo (cadastro, código por email, login, sessão, rate limit) ← **feito**
3. Layout base + telas de sementes e garimpo ← **feito**
4. Feedback e histórico ← **feito**
5. Retrospectiva semanal e loop de peso das pontes ← **feito**

As cinco etapas da spec estão fechadas.
