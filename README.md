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
uv run pytest          # testes unitários (Docker simulado; não exigem daemon)
uv run ruff check .    # lint
uv run mypy            # tipagem estrita
```

Testes de integração com o **Docker real** (constroem imagens, sobem containers e exigem
rede; levam alguns minutos):

```bash
uv run pytest -m docker -v
```

Eles percorrem o ciclo completo em projetos de exemplo — FastAPI + PostgreSQL + Redis
(incluindo `down` → `rollback` → `setup` com dados preservados e `purge`), Compose existente
com Dockerfile fora da aplicação e nginx, banco "do host" via `host.docker.internal` e
Next.js standalone — e terminam conferindo que não sobrou nenhum container, volume, rede ou
imagem e que o projeto ficou idêntico ao original. Cada teste usa um projeto Compose de nome
único e limpa apenas os próprios recursos, mesmo quando falha.

O CI (`.github/workflows/ci.yml`) roda lint, tipos e testes unitários em todo push e pull
request, e a integração com Docker em seguida.

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

# ... incluindo o conteúdo exato de cada arquivo (segredos ocultos)
dockerizer setup . --dry-run --diff

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
| `--diff` | `setup` | Mostra o conteúdo que seria gravado em cada arquivo (diff unificado; senhas, tokens e URLs com credenciais aparecem como `********`). Combine com `--dry-run` para revisar sem gravar. |
| `--host-ip` | `setup` | Endereço em que as portas são publicadas (ex.: `127.0.0.1`, `0.0.0.0`). Padrão: `127.0.0.1` se o projeto tem nginx, senão todas as interfaces. A escolha é lembrada. |
| `--images` | `rollback` | Remove também as imagens construídas pelo Dockerizer (nunca imagens base). |

## O que é gerado

| Arquivo | Conteúdo | Se já existir |
|---|---|---|
| `Dockerfile` (por aplicação) | Imagem base adequada, dependências instaladas antes do código (cache de build), usuário sem privilégios, comando identificado | Dockerfile do projeto é **reaproveitado** e nunca alterado |
| `compose.yaml` | Serviços, redes, volumes, healthchecks, dependências e políticas de reinicialização | Conflito: exige `--force` (com backup) |
| `.dockerignore` | Exclui `.env`, `.git`, dependências e caches do contexto de build | Preservado (com aviso se não excluir `.env`) |
| `.env.example` | Variáveis identificadas, **sem segredos** | Preservado |
| `.env` | Credenciais geradas localmente (permissão `600`) | Recebe **apenas** variáveis de infraestrutura ausentes; nada é sobrescrito |
| `.env.docker` | Só com serviços **do host**: as variáveis do `.env` com `localhost` trocado por `host.docker.internal` (permissão `600`) | Conflito: exige `--force` (com backup) |
| `.gitignore` | Acrescenta `.env`, `.dockerizer/` (e `.env.docker`, quando gerado) | Complementado (com backup) |

Decisões de segurança do Compose gerado:

- Bancos, cache e filas ficam numa rede **interna** (`data`, sem acesso externo) e **não são
  publicados no host**. Somente as aplicações web expõem portas.
- Se uma porta do host estiver ocupada, a próxima livre é proposta.
- Com nginx no projeto, as portas são publicadas só em `127.0.0.1`: publicá-las em todas as
  interfaces deixaria a aplicação acessível direto pela porta, sem TLS, limites ou
  autenticação do proxy.
- Healthchecks: se o código tem uma rota de saúde (`/health`, `/healthz`, `/ping`, rotas
  `api/health` do Next.js...), o teste é uma requisição HTTP (sem seguir redirecionamentos;
  qualquer status abaixo de 500 é saudável); senão, a porta TCP.
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

**Banco ou cache que já rodam na máquina.** Quando só o `.env` real aponta para
`localhost`/`127.0.0.1` (sem Compose existente), "local" é ambíguo: pode ser o PostgreSQL
instalado no servidor, com os dados reais. O Dockerizer pergunta, sem resposta padrão:

- `host`: nenhum container é criado; os containers acessam o serviço via
  `host.docker.internal` (`extra_hosts: host-gateway`), com as variáveis ajustadas em
  `.env.docker`. O `.env` original não muda, e os processos fora do Docker continuam
  funcionando. O serviço precisa aceitar conexões da rede Docker (PostgreSQL:
  `listen_addresses` e `pg_hba.conf`; Redis: `bind`/`protected-mode`). Migrações não rodam
  automaticamente.
- `container`: cria um serviço novo, com volume **vazio** (os dados atuais não são copiados).

As respostas ficam em `.dockerizer/manifest.json` e são reaproveitadas pelos próximos
`setup` (inclusive com `--yes`). Se o `.env` mudar depois, `up` e `status` avisam que o
`.env.docker` ficou desatualizado. Ao escolher `container` no lugar do banco do host, o
`setup` mostra os comandos `pg_dump`/`pg_restore` (ou `mysqldump`) para copiar os dados —
eles nunca são executados pelo Dockerizer.

### Projetos que já rodam fora do Docker

- **Compose existente**: Dockerfile, contexto e `target` de build de cada aplicação são
  reaproveitados, inclusive Dockerfiles fora da pasta da aplicação (ex.:
  `infra/docker/backend/Dockerfile` com contexto na raiz). Serviços que executam a imagem da
  aplicação (`image:` igual, `build:` próprio ou `extends`) viram processos com o mesmo
  `command:` (workers, pollers); serviços de migração viram a etapa de migração e serviços
  `restart: "no"` são apenas listados. Em produção, `docker-compose.prod.yml` tem prioridade;
  em desenvolvimento, o arquivo base, e comandos com reload viram o comando de
  desenvolvimento. Comandos com interpolação (`${VAR}`) são ignorados; `$$` é tratado como o
  `$` literal que chega ao container.
- **Comandos de produção**: units do systemd (`*.service`, `ExecStart=`), programas do
  supervisor (`[program:x]`) e o `ecosystem.config.js`/`ecosystem.json` do pm2 são lidos (nunca
  executados) e têm a mesma precedência do `Procfile`, que prevalece sobre eles. Os comandos
  são adaptados ao container: binários do virtualenv viram o nome do programa, `127.0.0.1`
  vira `0.0.0.0`, sockets Unix viram TCP, e `--daemon`, `--pid`, `--user` e arquivos de log
  são removidos. Processos que não são Celery (pollers, consumidores) viram serviços próprios.
- **Diretórios de dados**: variáveis como `STORAGE_PATH`, `MEDIA_ROOT`, `UPLOAD_DIR` e
  `UPLOAD_FOLDER` ganham um volume nomeado em todos os processos da aplicação, e o
  Dockerfile cria o diretório com o dono certo. O valor vem do `.env`, do `.env.example` ou
  do padrão literal no código (`BaseSettings`, `os.getenv(..., "padrão")`, `MEDIA_ROOT` do
  Django). Arquivos que já existem no host não são copiados para o volume.
- **nginx**: configurações do projeto são lidas e listadas (rotas, `proxy_pass` com a
  aplicação correspondente, TLS, `client_max_body_size` e outros ajustes), mas **não são
  reproduzidas**. Quando a porta publicada muda (ex.: 8000 ocupada → 8002), o Dockerizer
  indica qual `proxy_pass` ajustar.

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
- Django: `collectstatic`, `ALLOWED_HOSTS` e `DEBUG` não são configurados automaticamente
  (a análise avisa quando a configuração atual quebraria ou exporia a aplicação).
- Next.js com `output: "standalone"` gera uma imagem reduzida (`node server.js`); sem ele, a
  imagem usa `next start` com as dependências de produção.
- Dockerfiles existentes não recebem healthcheck nem são alterados; com contexto de build
  diferente da pasta da aplicação, o código não é montado no modo `--dev`.
- Dependências extraídas de `pyproject.toml` sem lockfile são copiadas para o Dockerfile:
  rode `dockerizer setup` novamente quando elas mudarem.
- No modo `--dev`, o container Python roda com UID 10001: arquivos criados pelo container no
  código montado podem ficar com outro dono.
- O rollback remove containers e redes; as imagens construídas só com `rollback --images`.
- URLs de conexão montadas pelo Compose assumem senhas sem caracteres especiais de URL (as
  geradas pelo Dockerizer já seguem essa regra).
- Projetos muito grandes são analisados até 20.000 arquivos e 8 níveis de diretório.
- nginx, Caddy e outros proxies não são gerados no Compose; TLS e domínios continuam no
  proxy do host. Arquivos de `/etc` fora do projeto não são lidos.
- systemd: especificadores (`%i`, `%h`) e units `Type=oneshot` são ignorados; supervisor:
  expansões `%(...)s` são ignoradas; pm2 em modo cluster vira um processo por serviço.
- Dados de diretórios do host e de bancos do host não são migrados para volumes.
- Algumas mensagens internas raras do Typer podem aparecer em inglês.

## Estrutura do código

```text
src/dockerizer/
├── cli/            # comandos (Typer), apresentação (Rich), tradução da ajuda
├── scanner/        # descoberta de arquivos e raízes de aplicações
├── analyzers/      # Python, Node.js, infraestrutura, variáveis, perguntas, systemd/
│                   # supervisor/pm2, Compose existente, nginx, rotas de saúde e dados
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
