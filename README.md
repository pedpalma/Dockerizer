# Dockerizer

Ferramenta de linha de comando que **containeriza projetos existentes**. Ela analisa a estrutura
do projeto, identifica aplicações, processos e serviços de infraestrutura, e gera Dockerfiles,
`compose.yaml`, `.dockerignore` e arquivos de ambiente — validando tudo e permitindo desfazer
as alterações com segurança.

A análise é **determinística** (regras e analisadores específicos, sem IA). Quando algo não
pode ser identificado com segurança — um comando de inicialização, se um banco é local ou
externo — o Dockerizer **pergunta** em vez de inventar.

## Requisitos

- Python 3.12 ou superior.
- Docker Engine com BuildKit (padrão desde o Docker 23).
- Docker Compose v2.24 ou superior (necessário para `env_file` com `required: false`).

O Docker só é necessário para `--build`, `--start`, `up`, `down`, `logs`, `status`,
`rollback` (remoção de containers) e `purge`. A análise, a geração e a validação estática
funcionam sem Docker.

## Instalação

```bash
# Com uv (recomendado)
uv tool install .

# Ou com pip, em um ambiente virtual
pip install .
```

Para desenvolvimento do próprio Dockerizer:

```bash
uv sync
uv run pytest          # testes (não exigem daemon Docker)
uv run ruff check .    # lint
uv run mypy            # tipagem estrita
```

## Tecnologias suportadas

| Categoria | Suporte |
|---|---|
| Python | FastAPI, Flask, Django, Celery (worker e beat), Alembic |
| Dependências Python | uv (`uv.lock`), Poetry, pip (`requirements*.txt`, `pyproject.toml`) |
| JavaScript/TypeScript | Node.js (Express, Fastify, Koa, NestJS…), Next.js, React (Vite e Create React App) |
| Dependências Node | npm, pnpm, yarn (v1 e Berry com `node_modules`) |
| Infraestrutura | PostgreSQL, MySQL/MariaDB, Redis, RabbitMQ |

Projetos com várias aplicações (ex.: `backend/` em Django e `frontend/` em React) são
suportados: cada aplicação recebe seu próprio Dockerfile.

## Exemplos de utilização

```bash
# Apenas analisar (nada é modificado)
dockerizer analyze .

# Ver o que seria feito, sem efeito algum
dockerizer setup . --dry-run

# Gerar e validar a configuração
dockerizer setup .

# Gerar, construir as imagens e iniciar os serviços
dockerizer setup . --build --start

# Ambiente de desenvolvimento (código montado no container e servidores com reload)
dockerizer setup . --dev --start

# Gerenciar os serviços
dockerizer status .
dockerizer logs . --service app --follow
dockerizer down .
dockerizer up .

# Desfazer tudo o que a ferramenta fez (volumes são preservados)
dockerizer rollback .
```

Exemplo de saída da análise de um projeto FastAPI + Celery + React:

```text
Aplicações
 app       .         python 3.12  fastapi, celery, alembic  uv    web :8000 → uvicorn app.main:app ...
                                                                  worker → celery -A app.worker worker ...
                                                                  migrações → alembic upgrade head
 frontend  frontend  node 22      react, vite               pnpm  web :8080 → nginx -g 'daemon off;'
Serviços de infraestrutura
 postgres  local  Configuração aponta para serviço local (evidência média).  app
 redis     local  Configuração aponta para serviço local (evidência média).  app
```

## Comandos disponíveis

| Comando | Descrição |
|---|---|
| `analyze` | Analisa o projeto sem modificá-lo. |
| `setup` | Analisa, gera os arquivos Docker e valida a configuração. |
| `up` | Inicia os serviços (`docker compose up --detach`). |
| `down` | Interrompe os serviços **sem remover volumes**. |
| `logs` | Exibe os logs (`--service`, `--follow`, `--tail`). |
| `validate` | Valida a configuração (estática e via `docker compose config`). |
| `status` | Exibe o estado e a saúde dos serviços. |
| `rollback` | Desfaz as alterações realizadas pela ferramenta. |
| `purge` | Remove **definitivamente** os volumes do projeto (confirmação própria). |

O caminho do projeto pode ser informado como argumento (`dockerizer setup ./meu-projeto`) ou
com `--path`. Sem nenhum dos dois, o diretório atual é usado.

## Parâmetros

| Parâmetro | Comandos | Descrição |
|---|---|---|
| `--path` | todos | Caminho do projeto. |
| `--dry-run` | `setup`, `up`, `down`, `rollback`, `purge` | Mostra as operações planejadas sem executá-las: nenhum arquivo é escrito e nenhum comando Docker que altere estado é executado. |
| `--yes`, `-y` | `setup`, `rollback` | Dispensa confirmações **não destrutivas** e aceita valores sugeridos. Perguntas sem valor padrão seguro continuam exigindo resposta. |
| `--force` | `setup`, `rollback` | Permite substituir arquivos preexistentes ou editados manualmente — **sempre com backup**. Nunca ignora proteções contra perda de dados. |
| `--build` | `setup` | Constrói as imagens. |
| `--start` | `setup` | Inicia os serviços. |
| `--dev` / `--production` | `setup` | Ambiente de desenvolvimento ou de produção (padrão). |
| `--report` | `analyze`, `setup` | Gera relatório (Markdown e JSON) em `.dockerizer/reports/`. |

## O que é gerado

| Arquivo | Conteúdo | Se já existir |
|---|---|---|
| `Dockerfile` (por aplicação) | Imagem base adequada, dependências instaladas antes do código (cache de build), usuário sem privilégios, comando identificado | Dockerfile do projeto é **reaproveitado** e nunca alterado |
| `compose.yaml` | Serviços, redes, volumes, healthchecks, dependências e políticas de reinicialização | Conflito: exige `--force` (com backup) |
| `.dockerignore` | Exclui `.env`, `.git`, dependências e caches do contexto de build | Preservado (com aviso se não excluir `.env`) |
| `.env.example` | Variáveis identificadas, **sem segredos** | Preservado |
| `.env` | Credenciais geradas localmente (permissão `600`) | Recebe **apenas** variáveis de infraestrutura ausentes; nada é sobrescrito |
| `.gitignore` | Acrescenta `.env` e `.dockerizer/` | Complementado (com backup) |

Decisões de segurança do Compose gerado:

- Bancos, cache e filas ficam numa rede **interna** (`data`, sem acesso externo) e **não são
  publicados no host**. Somente as aplicações web expõem portas.
- Se uma porta do host estiver ocupada, a próxima livre é proposta.
- Containers de aplicação usam `no-new-privileges`, `init` e usuário não root.
- Senhas vêm do `.env` via interpolação obrigatória (`${VAR:?}`), nunca do Dockerfile.
- Migrações (Alembic/Django) rodam num serviço próprio antes da aplicação **apenas quando o
  banco é local** — nunca contra um banco externo.

### Como os serviços de infraestrutura são decididos

A presença de uma dependência (ex.: `psycopg`, `redis`) **não basta** para criar um serviço.
As evidências são classificadas:

1. **Fortes** — `.env` real e arquivos Compose existentes;
2. **Médias** — `.env.example`, URLs e configurações no código (ex.: `ENGINE` do Django);
3. **Fracas** — apenas dependências.

URLs que apontam para `localhost` ou nomes de serviço indicam infraestrutura local; hosts
externos (ex.: `*.rds.amazonaws.com`) indicam serviço externo, e nenhum container é criado.
Sem evidência conclusiva (ou com evidências conflitantes), o Dockerizer pergunta.

## Processo de reversão

Toda alteração é registrada em `.dockerizer/`:

```text
.dockerizer/
├── manifest.json     # arquivos criados/modificados, hashes SHA-256, execuções, recursos Docker
├── credentials.env   # credenciais da infraestrutura local (mantidas enquanto houver volumes)
├── backups/          # cópias dos originais antes de qualquer modificação
└── reports/          # relatórios gerados com --report
```

`dockerizer rollback`:

1. Identifica as alterações registradas no manifesto.
2. Remove os containers e redes do projeto Compose registrado (`docker compose down`, sem `-v`,
   e remoção do que restar pelos labels do projeto **e** `com.dockerizer.managed=true` —
   containers criados por outro compose nunca são removidos).
3. Remove os arquivos criados pela ferramenta **se não foram editados depois**.
4. Restaura os arquivos modificados a partir do backup (conferindo o hash do backup e as
   permissões originais).
5. **Preserva volumes e dados persistentes.**
6. Detecta modificações manuais posteriores e as informa como **conflitos**, mantendo os
   arquivos. Com `--force`, a versão editada é salva em `.dockerizer/backups/` antes da
   reversão.

`.dockerizer/` só é removido quando nada mais depende dele (sem volumes preservados, sem
backups de versões editadas e sem conflitos pendentes).

O Dockerizer **nunca** executa operações globais como `docker system prune`, e uma guarda
interna bloqueia `docker compose down -v` e remoção de volumes fora do comando `purge`.

## Cuidados com dados persistentes

- `down` e `rollback` **nunca** apagam volumes.
- As credenciais dos bancos são guardadas em `.dockerizer/credentials.env` e **reutilizadas**
  em um novo `setup`. Isso é importante: o PostgreSQL e o MySQL definem a senha apenas na
  primeira inicialização do volume — gerar uma senha nova tornaria os dados inacessíveis.
- Não altere manualmente as senhas do `.env` depois que os volumes foram criados.
- Para apagar os dados definitivamente, use o comando separado:

  ```bash
  dockerizer down .
  dockerizer purge .     # exige digitar o nome do projeto Compose; --yes não se aplica
  ```

  O `purge` só remove volumes do projeto Compose registrado e declarados pelo Dockerizer, e
  recusa-se a executar enquanto houver containers do projeto.
- Faça backup dos dados (ex.: `pg_dump`) antes de qualquer `purge`.

## Segurança

- Escrita restrita ao diretório do projeto; caminhos com `..`, absolutos ou através de links
  simbólicos são recusados.
- Nenhum arquivo é sobrescrito silenciosamente (hash SHA-256 de tudo que foi gerado).
- Scripts do projeto (`start.sh`, `entrypoint.sh`, etc.) são apenas listados — **nunca
  executados** durante a análise.
- Comandos externos são executados sem shell, como lista de argumentos e com timeout.
- O projeto Compose gerado se chama `<diretório>-dockerizer` (ex.: `loja-dockerizer`), nunca o
  nome do diretório: projetos já dockerizados usam esse nome por padrão, e compartilhá-lo faria
  o Compose recriar ou remover (como órfãos) os containers do compose original.
- Se a raiz já tem um `docker-compose.yml` (ou `compose.yml`), gerar `compose.yaml` é um
  **conflito**, porque o Docker Compose passaria a priorizá-lo. Com `--force` o arquivo é
  gerado e o original não é tocado; o `rollback` remove o `compose.yaml` gerado.
- Relatórios mostram apenas nomes de variáveis; valores sensíveis nunca são exibidos.
- `.npmrc` com tokens não é copiado para a imagem.

## Limitações conhecidas

- Pipenv, Ruby, Go, Java e outras tecnologias não são suportados nesta versão.
- Monorepos com *workspaces* (npm/pnpm/yarn) têm suporte limitado; Yarn PnP não é suportado
  (use `nodeLinker: node-modules`).
- Os healthchecks das aplicações verificam a porta TCP, não um endpoint HTTP específico.
- Django: `collectstatic`, `ALLOWED_HOSTS` e `DEBUG` não são configurados automaticamente.
- Next.js usa `next start` (o modo `output: "standalone"` não é aproveitado, gerando imagens
  maiores).
- Dependências extraídas de `pyproject.toml` sem lockfile são copiadas para o Dockerfile:
  rode `dockerizer setup` novamente quando elas mudarem.
- No modo `--dev`, o container Python roda com UID 10001: arquivos criados pelo container no
  código montado podem ficar com outro dono.
- O rollback remove containers e redes, mas não as imagens construídas. Para removê-las:
  `docker image rm <projeto>-<aplicação>`.
- URLs de conexão montadas pelo Compose assumem senhas sem caracteres especiais de URL (as
  geradas pelo Dockerizer já seguem essa regra).
- Projetos muito grandes são analisados até 20.000 arquivos e 8 níveis de diretório.
- Algumas mensagens internas raras do Typer podem aparecer em inglês.

## Estrutura do código

```text
src/dockerizer/
├── cli/            # comandos (Typer), apresentação (Rich), tradução da ajuda
├── scanner/        # descoberta de arquivos e raízes de aplicações
├── analyzers/      # Python, Node.js, infraestrutura, variáveis, perguntas
├── models/         # modelo de domínio (Pydantic)
├── generators/     # Dockerfile, Compose, .env, plano de geração
├── templates/      # templates Jinja2
├── fs/             # caminhos seguros, hashes, planejamento/aplicação de mudanças
├── docker/         # wrapper da Docker CLI com guarda contra operações destrutivas
├── rollback/       # manifesto, reversão e purge
├── validators/     # validação estática e via docker compose config
└── reporting/      # relatórios Markdown/JSON
```

Para adicionar uma tecnologia: crie um analisador em `analyzers/` (Python e Node.js servem de
modelo), registre-o em `analyzers/project.py` e, se for uma nova linguagem, adicione um template
de Dockerfile em `templates/`. Novos serviços de infraestrutura são declarados em
`analyzers/infrastructure.py` (`SPECS`) e `generators/compose.py` (`INFRA_VARS` e `WIRING`).
