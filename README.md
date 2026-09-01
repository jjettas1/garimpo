# Garimpo

Acha artista pouco conhecido a partir do que você já escuta — e mostra
**o caminho** que ligou um ao outro.

```
Duster ──0.87──> Bedhead ──0.64──> Rex
        semente          ponte           descoberta
```

Essa rota é o produto. Sem ela, seria só mais uma lista de recomendação.

## Como funciona o motor

1. Cada **semente** sua vira uma consulta de similares no Last.fm
2. Os melhores vizinhos de cada semente viram **pontes**, e a gente atravessa
   cada ponte pedindo os similares *dela*
3. Cada caminho ganha um peso: `match₁ × match₂ × decaimento × peso da ponte`
4. Quando o mesmo artista chega por caminhos diferentes, os pesos se juntam:
   `afinidade = 1 - Π(1 - wᵢ)`
5. Sobra só quem está dentro da sua faixa de ouvintes
6. `score = afinidade × raridade^0.7 × coerência^0.5`

O **peso da ponte** é o que faz o sistema aprender: cada aprovar/pular/banir
atualiza o placar daquela ponte pra você, com suavização de Laplace
(`(aprovados + 1) / (total + 2)`). Ponte nova entra neutra. Nada de machine
learning — a conta é determinística e dá pra explicar linha por linha.

## Rodando

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # e coloque sua LASTFM_API_KEY
alembic upgrade head
```

Pegue a chave em <https://www.last.fm/api/account/create>.

## Rodando o servidor

```bash
uvicorn app.web:app --reload
```

Em <http://127.0.0.1:8000> o app está inteiro: criar conta, cadastrar
sementes com autocomplete, garimpar, dar veredito, histórico e retrospectiva.

Criar conta já entra — não há email pra confirmar.

### As telas

| rota | o que faz |
|---|---|
| `/` | explica o conceito e mostra uma rota de verdade |
| `/cadastro`, `/entrar` | email e senha, nada mais — cadastrar já entra |
| `/sementes` | 3 a 8 artistas com autocomplete no last.fm, e a faixa de ouvintes |
| `/garimpo` | uma descoberta por vez, com a rota inteira e os três vereditos |
| `/historico` | tudo que foi aprovado, com a rota preservada |
| `/retrospectiva` | por semana: aprovados, taxa, média de ouvintes e ranking das pontes |

### Visual

Terminal escuro, e a sério — não é tema alternativo. Fundo `#080b09`, verde
fósforo `#00e05f` na ação, três níveis de verde no texto, bordas hairline.
JetBrains Mono e HTMX são **self-hosted** em `app/static/`: nenhum CDN,
nenhum Google Fonts. Sem gradiente, sombra ou animação decorativa — a única
coisa que se move é o ponto que indica rodada em andamento.

## Usando o motor pela linha de comando

```bash
python -m app.cli usuario  --email eu@exemplo.com --senha uma-senha-boa
python -m app.cli sementes --email eu@exemplo.com "Duster" "Slint" "Codeine"
python -m app.cli faixa    --email eu@exemplo.com --piso 500 --teto 80000
python -m app.cli garimpar --email eu@exemplo.com --limite 10
python -m app.cli listar   --email eu@exemplo.com
```

O `garimpar` demora: são centenas de chamadas ao Last.fm, seguradas em 4
requisições por segundo pra não estourar o limite deles. Medido com 3
sementes (Duster, Slint, Codeine) e faixa de 1k a 100k ouvintes:

| rodada | tempo | por quê |
|---|---|---|
| primeira | 72s | 226 artistas buscados na API |
| segunda | 7s | os 226 vieram do cache |

O cache vale 14 dias. É por isso que o garimpo **não pode** rodar dentro do
request HTTP — 72 segundos derrubariam qualquer timeout. Na etapa 3 isso vai
pro `BackgroundTasks` com a tela em polling.

## O loop, em uma frase

As sementes dizem seu gosto, o Last.fm dá o mapa, e o feedback ajusta o peso
de cada ponte — só isso separa a sua segunda rodada da de outra pessoa com as
mesmas sementes. A `/retrospectiva` mostra esses pesos: são os mesmos números
que o motor usa pra pontuar, não uma cópia recalculada.

## Sobre o feedback

Cada aprovar/pular/banir atualiza o placar da ponte que trouxe aquele
artista, e é só isso que personaliza as próximas rodadas.

`bridge_stats` é materializado — ninguém recalcula depois. Por isso trocar de
veredito **move** o placar em vez de somar de novo, e repetir o mesmo veredito
não faz nada. Sem esse cuidado, um clique duplo estragava o peso da ponte pra
sempre.

## Sobre o SQLite

O garimpo roda numa tarefa de fundo enquanto a tela faz polling: dois
escritores no mesmo arquivo. O engine liga **WAL** e um timeout de 30s por
isso — sem eles a primeira rodada morre com `database is locked`, e a tela
fica em "garimpando…" pra sempre.

## Sobre a autenticação

Feita na mão, sem biblioteca pronta. Não há verificação por email: quem
cadastra entra na hora.

**Senha com argon2id, token de sessão com sha256.** Parece contradição, mas
não é. Senha é curta e adivinhável: sem um hash lento, um vazamento do banco
vira lista de senhas em horas. Token de sessão são 32 bytes sorteados — não
existe dicionário contra 256 bits de entropia. E o sal aleatório do argon2
tornaria impossível *procurar* a sessão pelo hash.

**O login nunca diz qual dos dois campos errou.** "Email não cadastrado" e
"senha incorreta" são a mesma frase, byte a byte. E quando o email não
existe, a senha é conferida contra um hash falso — senão a resposta voltaria
instantânea e o cronômetro entregaria quem tem conta aqui.

**O cadastro, esse revela.** Ele precisa dizer se criou a conta ou não, e
"criou" significa "esse email não tinha dono". Não dá pra evitar sem
verificação por email. O login, que é onde alguém tentaria entrar, continua
sem vazar nada.

## Deploy

O `render.yaml` na raiz descreve o serviço. No painel do Render: **New →
Blueprint**, aponte pro repositório, e preencha `LASTFM_API_KEY` (é a única
variável marcada `sync: false`, ou seja, que não vem do arquivo).

### Por que Render e não Vercel

Vercel e afins rodam funções serverless, e este app precisa de três coisas
que serverless não dá:

| precisa de | por quê |
|---|---|
| disco que persiste | o banco é um arquivo SQLite |
| processo que fica de pé | o garimpo leva ~70s numa tarefa de fundo |
| memória entre requisições | o limite de 4 req/s do Last.fm é do processo |

Em serverless o banco sumiria a cada requisição, o garimpo seria morto antes
de terminar, e cada instância teria seu próprio orçamento de requisições —
estourando o teto do Last.fm.

### Os dois bancos

O app fala SQLite e Postgres, e a escolha é só a `DATABASE_URL`:

| onde | banco | por quê |
|---|---|---|
| sua máquina | `sqlite:///garimpo.db` | zero configuração |
| Render pago | `sqlite:////var/data/garimpo.db` | disco persistente, um arquivo só |
| Render grátis | `postgresql://...` (Neon, Supabase) | sem disco, o SQLite seria apagado a cada deploy |

O `render.yaml` vem configurado pro caminho gratuito. As diferenças de
dialeto ficam todas no `app/db.py`:

- URL de provedor vira URL que o SQLAlchemy aceita — `postgres://` e
  `postgresql://` viram `postgresql+psycopg://`
- Postgres gerenciado derruba conexão ociosa, então liga `pool_pre_ping`
- SQLite ganha WAL, timeout e o pragma de chave estrangeira; o Postgres não
  leva nada disso

Pra conferir contra um Postgres de verdade antes de subir:

```bash
TEST_DATABASE_URL='postgresql://...' pytest tests/test_migracoes.py
```

Esse teste **apaga e recria o schema**, então aponte pra um banco
descartável. Sem a variável ele é pulado.

### Sobre o plano gratuito

O serviço dorme por inatividade e demora a acordar. Uma rodada de garimpo
deixada sozinha pode ser interrompida no meio — o app detecta rodada
abandonada e oferece tentar de novo, em vez de ficar preso em
"garimpando…".

## Testes

```bash
pytest
```

Nenhum teste bate na API de verdade — o `tests/conftest.py` tem um grafo de
mentira com uma semente, duas pontes e três candidatos (um dentro da faixa,
um famoso demais, um obscuro demais).

## Organização

```
app/
  config.py    tudo que vem de variável de ambiente
  db.py        engine e sessão
  models.py    as 9 tabelas
  lastfm.py    cliente da API (rate limit) + cache no banco
  garimpo.py   o motor: matemática pura em cima, orquestração embaixo
  cli.py       rodar sem tela
migrations/    alembic
tests/         pytest
```

A chave do Last.fm só existe dentro de `lastfm.py`. Nunca vai pro template
nem pro JavaScript.

## Etapas

| # | o quê | status |
|---|---|---|
| 1 | estrutura, config, migrations, motor por CLI | pronto |
| 2 | auth (cadastro, código por email, sessão, rate limit) | pronto |
| 3 | layout base + telas de sementes e garimpo | pronto |
| 4 | feedback e histórico | pronto |
| 5 | retrospectiva semanal e loop de peso das pontes | pronto |

## Uma pendência

O `garimpo.py` original mencionado na especificação não existe neste
repositório. O motor em `app/garimpo.py` foi escrito a partir do algoritmo
descrito em `PROMPT-CLAUDE-CODE.md`. Se o arquivo original aparecer, vale
comparar — principalmente a constante de decaimento por salto e a fórmula de
raridade, que a spec descreve mas não fixa em número.
