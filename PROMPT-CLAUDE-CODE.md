# Projeto: Garimpo — descoberta de artistas underground com rota rastreável

## Contexto

Quero construir uma aplicação web que encontra artistas pouco conhecidos a
partir dos artistas que a pessoa já escuta, e **mostra o caminho** que ligou
um ao outro. A rota de descoberta é o diferencial do produto, não um detalhe:
o usuário precisa entender por que aquele nome apareceu pra ele.

Já existe um motor funcionando em `garimpo.py` (na raiz do projeto). Ele
percorre o grafo de similaridade do Last.fm, guarda as rotas e pontua os
candidatos. **Leia esse arquivo antes de escrever qualquer coisa** — o
algoritmo dele é a base e não deve ser reinventado, só refatorado pra caber
na aplicação.

## Stack (já decidida, não sugerir alternativa)

- Python 3.11+, FastAPI
- SQLite via SQLAlchemy 2.x (async não é necessário)
- Jinja2 renderizando HTML no servidor + HTMX pra interações sem reload
- Alembic pra migrations
- Sem React, sem SPA, sem build step de frontend
- Sem Docker por enquanto
- Sem integração com Spotify em lugar nenhum — o Last.fm é a única fonte
  externa de dados musicais

## Identidade visual

Terminal escuro, tipo tela de código em filme de hacker. Não é ironia nem
tema alternativo — é a cara do produto.

- Fundo quase preto (`#080b09`), superfícies em `#0b0f0c`
- Verde fósforo como cor de ação e destaque (`#00e05f`), verdes dessaturados
  para texto secundário (`#4f8f69`) e terciário (`#2f5c45`)
- Bordas hairline em `#1d3527`
- Fonte monoespaçada em toda a interface (JetBrains Mono ou similar,
  self-hosted, sem Google Fonts)
- Sem gradiente, sem glow, sem sombra, sem animação decorativa
- Tudo em caixa baixa nos rótulos de interface
- Um único arquivo CSS com as cores em variáveis CSS no `:root`

O layout precisa ser legível e profissional, não uma paródia de terminal.
Espaçamento generoso, hierarquia clara, nada de ASCII art.

## Modelo de dados

```
users              id, email, password_hash, email_verificado, criado_em
verification_codes id, user_id, codigo_hash, tipo, expira_em, usado_em, tentativas
sessions           id, user_id, token_hash, expira_em, criado_em, user_agent
seed_artists       id, user_id, nome, mbid, ordem
artists            id, nome, nome_norm (unique), mbid, listeners, playcount,
                   tags_json, atualizado_em
discoveries        id, user_id, artist_id, rota_json, ponte_id, score,
                   afinidade, raridade, coerencia, n_rotas, semana, criado_em
feedback           id, discovery_id, veredito, criado_em
bridge_stats       id, user_id, artist_id, aprovados, total
runs               id, user_id, status, iniciado_em, terminado_em, erro
```

- `veredito` é enum: `aprovado`, `pulado`, `banido`
- `semana` no formato `2026-W34`, usado pra agrupar a retrospectiva
- `artists` funciona como cache do Last.fm; não bater na API se
  `atualizado_em` tem menos de 14 dias
- `bridge_stats` é derivado do feedback, mas materializado pra não recalcular
  a cada garimpo

## Autenticação

Construir na mão, sem biblioteca de auth pronta. Requisitos que não são
negociáveis:

- Senha com **argon2id** (`argon2-cffi`). Nunca sha256, nunca bcrypt puro.
- Cadastro envia código numérico de 6 dígitos por email. O código é guardado
  **hasheado** no banco, expira em 15 minutos, e é invalidado após 5
  tentativas erradas.
- Rate limit no envio de código: máximo 3 por email por hora, 10 por IP por
  hora. Implementar em tabela mesmo, sem Redis.
- As mensagens de erro de "email não cadastrado" e "senha incorreta" devem
  ser **byte a byte idênticas**, e o tempo de resposta das duas também
  (comparar sempre contra um hash dummy quando o usuário não existe). Sem
  enumeração de usuários.
- Sessão por cookie `HttpOnly`, `SameSite=Lax`, `Secure` em produção. Token
  aleatório de 32 bytes, guardado hasheado.
- Logout invalida a sessão no servidor, não só apaga o cookie.
- Envio de email via Resend (chave em variável de ambiente). Abstrair atrás
  de uma interface `EmailSender` com uma implementação `ConsoleEmailSender`
  que só printa o código no terminal, pra desenvolvimento.

## Motor de descoberta

Refatorar `garimpo.py` em módulos dentro do pacote da aplicação, mantendo o
algoritmo:

1. Seeds do usuário → `artist.getSimilar` no Last.fm (fanout 30)
2. Os melhores vizinhos de cada seed viram **pontes**; atravessa cada uma
   pedindo os similares dela
3. Peso de uma rota = produto dos matches × decaimento por salto × peso da
   ponte
4. Quando o mesmo artista chega por rotas diferentes, combina:
   `afinidade = 1 - Π(1 - wᵢ)`
5. Filtra por faixa de ouvintes (piso e teto configuráveis por usuário)
6. `score = afinidade × raridade^0.7 × coerência^0.5`, onde raridade é
   logarítmica na contagem de ouvintes e coerência é o overlap de tags com
   as tags das seeds

O peso da ponte vem do `bridge_stats` daquele usuário, com suavização de
Laplace: `(aprovados + 1) / (total + 2)`, normalizado pra 1.0 quando neutro.
Ponte sem histórico começa neutra.

O garimpo **não roda dentro do request HTTP** — são centenas de chamadas ao
Last.fm. Usar `BackgroundTasks` do FastAPI gravando estado na tabela `runs`,
e a tela ficar em polling via HTMX até `status = pronto`. Não trazer Celery
nem RQ pra isso.

Respeitar o rate limit do Last.fm (máximo 5 req/s, deixar folga em 4).

## Telas

1. **`/` landing** — explica o conceito em três frases e mostra um exemplo
   de rota. Sem login.
2. **`/cadastro` e `/entrar`** — formulário mínimo. Cadastro leva pra tela de
   confirmação de código.
3. **`/confirmar`** — seis campos de um dígito, com opção de reenviar.
4. **`/sementes`** — o usuário adiciona de 3 a 8 artistas, com autocomplete
   batendo no `artist.search` do Last.fm. Também é onde ele ajusta piso e
   teto de ouvintes.
5. **`/garimpo`** — dispara uma rodada e mostra as descobertas uma de cada
   vez, em card: nome, contagem de ouvintes, a rota completa com as setas,
   tags, e os três botões (`aprovar`, `pular`, `nunca mais`). Aprovar não
   recarrega a página.
6. **`/historico`** — tudo que já foi aprovado, com a rota preservada.
7. **`/retrospectiva`** — por semana: quantos aprovados, taxa de aprovação,
   média de ouvintes dos aprovados, e o ranking das pontes que mais renderam.

## Ordem de construção

Fazer nessa sequência e **parar pra eu revisar no fim de cada etapa**. Não
adiantar etapas.

1. Estrutura do projeto, config por variável de ambiente, migrations, e o
   motor refatorado rodando por linha de comando contra o banco novo
2. Auth completo (cadastro, código por email, login, sessão, logout,
   rate limit) com testes
3. Layout base e as telas de seeds e garimpo
4. Feedback e histórico
5. Retrospectiva semanal e o loop de peso das pontes

## Convenções

- Instruções do projeto ficam em `AGENTS.md` na raiz; `CLAUDE.md` só importa
  ele com `@AGENTS.md`
- Todo o código Python numa pasta `app/`, com os módulos separados por
  responsabilidade (`auth/`, `discovery/`, `web/`, `models/`)
- Segredos só por variável de ambiente, com um `.env.example` versionado
- Testes com pytest. Cobrir obrigatoriamente: o cálculo de score, a combinação
  de rotas múltiplas, a expiração e invalidação de código, e o rate limit.
  Não testar a API do Last.fm de verdade — usar fixtures.
- Commits pequenos e descritivos, em português

## O que não fazer

- Não adicionar autenticação social (Google, Spotify, GitHub)
- Não criar sistema de recomendação com machine learning; o scoring é
  determinístico e explicável de propósito
- Não colocar biblioteca de componentes nem framework CSS
- Não gerar dados de exemplo falsos no banco de produção
- Não criar endpoints de API pública ou documentação OpenAPI voltada pra
  terceiros; é um app renderizado no servidor
- Não expor a chave do Last.fm no HTML nem em JavaScript

## Primeiro passo

Comece lendo `garimpo.py`, depois me apresente a estrutura de pastas e o
schema do banco que você pretende criar, **antes de escrever código**. Quero
aprovar isso primeiro.
