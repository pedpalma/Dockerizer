# Changelog

Todas as mudanças relevantes do Dockerizer. O formato segue o
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o projeto usa
[versionamento semântico](https://semver.org/lang/pt-BR/).

Ao publicar uma versão, renomeie a seção "Não lançado" para `[X.Y.Z] - AAAA-MM-DD`: o workflow
de release usa essa seção como notas da versão (veja "Publicando uma versão" no README).

## [Não lançado]

## [0.1.0] - 2026-11-05

Primeira versão.

### Análise

- Projetos Python (FastAPI, Flask, Django, Celery; uv, Poetry e pip) e Node.js (Next.js,
  React/Vite; npm, pnpm e Yarn), inclusive com várias aplicações no mesmo repositório.
- Serviços de infraestrutura (PostgreSQL, MySQL/MariaDB, Redis, RabbitMQ) decididos por níveis
  de evidência; casos ambíguos — como um banco que pode já rodar na máquina — viram perguntas,
  e as respostas são lembradas.
- Projetos que rodam fora do Docker: comandos de systemd, supervisor, pm2 e Procfile; rotas
  do nginx listadas sem serem reproduzidas; diretórios de dados viram volumes nomeados.
- Compose e Dockerfiles existentes são reaproveitados (contexto, `target` e workers que só
  existem no Compose).
- Scripts encontrados no projeto nunca são executados.

### Geração

- Dockerfile por aplicação (cache de dependências, usuário sem privilégios, Next.js
  standalone), `compose.yaml`, `.dockerignore`, `.env.example` e `.env` com credenciais
  geradas localmente.
- Bancos e filas numa rede interna, nunca publicados no host; portas em `127.0.0.1` quando
  há nginx (`--host-ip` para escolher).
- Healthchecks HTTP quando há rota de saúde; migrações em serviço próprio.
- Serviços do host acessados via `host.docker.internal`, com `.env.docker` gerado a partir do
  `.env` sem alterá-lo.
- Modo `--dev` com código montado, servidores com reload e o UID/GID do usuário do host.
- `setup --diff` mostra o conteúdo de cada arquivo antes de gravar, com segredos ocultos.

### Operação e segurança

- Comandos `analyze`, `setup`, `up`, `down`, `logs`, `validate`, `status`, `rollback`,
  `backup`, `restore` e `purge`, com mensagens em português.
- Nenhum arquivo é sobrescrito sem backup; o `rollback` só desfaz o que não foi editado
  depois e nunca remove volumes.
- `purge` separado, com confirmação digitada e oferta de backup antes.
- `backup`/`restore` lógicos de PostgreSQL, MySQL/MariaDB e Redis, com SHA-256 conferido e
  backup de segurança antes de restaurar; backups nunca são apagados pela ferramenta.
- Projeto Compose isolado (`<diretório>-dockerizer`) e recursos marcados com label; nenhuma
  operação global (`prune`) e `down -v` bloqueado por uma guarda.
- Exige Docker Compose 2.24 ou superior, com mensagem clara em versões antigas.

[Não lançado]: https://github.com/pedpalma/Dockerizer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/pedpalma/Dockerizer/releases/tag/v0.1.0
