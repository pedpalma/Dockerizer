"""Geração do compose.yaml.

O documento é montado como estrutura de dados e serializado com PyYAML (garantindo YAML
válido); o template Jinja2 adiciona apenas o cabeçalho explicativo.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from dockerizer.analyzers.infrastructure import SPECS
from dockerizer.constants import MANAGED_LABEL, compose_project_name
from dockerizer.generators.dockerfile import MODE_LABELS, main_process
from dockerizer.generators.templating import render
from dockerizer.models.architecture import (
    Application,
    InfraService,
    Language,
    Mode,
    Process,
    ProcessKind,
    ProjectAnalysis,
    ServiceKind,
)

APP_NETWORK = "app"
DATA_NETWORK = "data"
HOST_GATEWAY = "host.docker.internal"
HOST_ACCESS_HINTS = {
    ServiceKind.POSTGRES: "listen_addresses no postgresql.conf e uma regra no pg_hba.conf",
    ServiceKind.MYSQL: "bind-address no my.cnf e um usuário com permissão para o host",
    ServiceKind.REDIS: "bind e protected-mode no redis.conf",
    ServiceKind.RABBITMQ: "um usuário diferente de guest (guest só aceita localhost)",
}


class _NoAliasDumper(yaml.SafeDumper):
    """Evita âncoras/aliases (&id001) no YAML gerado, mantendo-o legível."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


@dataclass(frozen=True)
class InfraVar:
    name: str
    default: str | None  # None = segredo gerado localmente
    description: str


INFRA_VARS: dict[ServiceKind, tuple[InfraVar, ...]] = {
    ServiceKind.POSTGRES: (
        InfraVar("POSTGRES_USER", "app", "usuário do PostgreSQL"),
        InfraVar("POSTGRES_PASSWORD", None, "senha do PostgreSQL"),
        InfraVar("POSTGRES_DB", "{db}", "nome do banco"),
    ),
    ServiceKind.MYSQL: (
        InfraVar("MYSQL_USER", "app", "usuário do MySQL"),
        InfraVar("MYSQL_PASSWORD", None, "senha do MySQL"),
        InfraVar("MYSQL_ROOT_PASSWORD", None, "senha do usuário root do MySQL"),
        InfraVar("MYSQL_DATABASE", "{db}", "nome do banco"),
    ),
    ServiceKind.REDIS: (),
    ServiceKind.RABBITMQ: (
        InfraVar("RABBITMQ_DEFAULT_USER", "app", "usuário do RabbitMQ"),
        InfraVar("RABBITMQ_DEFAULT_PASS", None, "senha do RabbitMQ"),
    ),
}

# Nomes de variáveis que a aplicação pode ler para se conectar a cada serviço.
WIRING: dict[ServiceKind, dict[str, tuple[str, ...]]] = {
    ServiceKind.POSTGRES: {
        "url": (
            "DATABASE_URL",
            "DB_URL",
            "DATABASE_URI",
            "SQLALCHEMY_DATABASE_URI",
            "POSTGRES_URL",
            "POSTGRESQL_URL",
            "PG_URL",
        ),
        "host": ("DB_HOST", "DATABASE_HOST", "POSTGRES_HOST", "PGHOST"),
        "port": ("DB_PORT", "DATABASE_PORT", "POSTGRES_PORT", "PGPORT"),
        "name": ("DB_NAME", "DATABASE_NAME", "POSTGRES_DB", "PGDATABASE"),
        "user": ("DB_USER", "DB_USERNAME", "DATABASE_USER", "POSTGRES_USER", "PGUSER"),
        "password": (
            "DB_PASSWORD",
            "DB_PASS",
            "DATABASE_PASSWORD",
            "POSTGRES_PASSWORD",
            "PGPASSWORD",
        ),
    },
    ServiceKind.MYSQL: {
        "url": ("DATABASE_URL", "DB_URL", "DATABASE_URI", "SQLALCHEMY_DATABASE_URI", "MYSQL_URL"),
        "host": ("DB_HOST", "DATABASE_HOST", "MYSQL_HOST"),
        "port": ("DB_PORT", "DATABASE_PORT", "MYSQL_PORT"),
        "name": ("DB_NAME", "DATABASE_NAME", "MYSQL_DATABASE"),
        "user": ("DB_USER", "DB_USERNAME", "DATABASE_USER", "MYSQL_USER"),
        "password": ("DB_PASSWORD", "DB_PASS", "DATABASE_PASSWORD", "MYSQL_PASSWORD"),
    },
    ServiceKind.REDIS: {
        "url": ("REDIS_URL", "REDIS_URI", "CACHE_URL", "CACHE_REDIS_URL"),
        "host": ("REDIS_HOST",),
        "port": ("REDIS_PORT",),
        "broker": ("CELERY_BROKER_URL", "BROKER_URL"),
        "backend": ("CELERY_RESULT_BACKEND", "RESULT_BACKEND"),
    },
    ServiceKind.RABBITMQ: {
        "url": ("RABBITMQ_URL", "AMQP_URL", "RABBITMQ_URI"),
        "host": ("RABBITMQ_HOST", "AMQP_HOST"),
        "port": ("RABBITMQ_PORT", "AMQP_PORT"),
        "broker": ("CELERY_BROKER_URL", "BROKER_URL"),
    },
}


@dataclass
class PortBinding:
    service: str
    container_port: int
    host_port: int


@dataclass
class ComposeResult:
    content: str
    document: dict[str, Any]
    services: list[str]
    images: list[str]
    volumes: list[str]
    networks: list[str]
    ports: list[PortBinding]
    env_overrides: dict[str, dict[str, str]]
    warnings: list[str] = field(default_factory=list)


def service_names(app: Application) -> dict[str, str]:
    """Nome do serviço Compose para cada processo da aplicação."""
    names: dict[str, str] = {}
    main = main_process(app)
    for process in app.processes:
        names[process.name] = app.name if process is main else f"{app.name}-{process.name}"
    return names


def compose_argv(argv: list[str]) -> list[str]:
    """Escapa "$" para o Compose não interpolar o que deve chegar ao shell do container."""
    return [arg.replace("$", "$$") for arg in argv]


def build_section(app: Application) -> dict[str, Any]:
    """Contexto e Dockerfile: os do Compose existente, quando declarados, ou os da aplicação."""
    context = app.build_context or app.path
    dockerfile = "Dockerfile"
    if app.existing_dockerfile:
        dockerfile = posixpath.relpath(app.existing_dockerfile, context)
    build: dict[str, Any] = {"context": context, "dockerfile": dockerfile}
    if app.build_target:
        build["target"] = app.build_target
    return build


def migrate_service_name(app: Application) -> str:
    return f"{app.name}-migrate"


def image_name(project: str, app: Application) -> str:
    return f"{project}-{app.name}"


def postgres_data_path(image: str) -> str:
    # Imagens derivadas (timescale/timescaledb:2.x-pg17, pgvector/pgvector:pg17) trazem a
    # versão do PostgreSQL após "pg"; na imagem oficial ela é o início da tag.
    match = re.search(r"[:-]pg(\d+)", image) or re.search(r":(\d+)", image)
    if match and int(match.group(1)) >= 18:
        return "/var/lib/postgresql"
    return "/var/lib/postgresql/data"


def _required(name: str) -> str:
    return f"${{{name}:?Defina {name} no arquivo .env (veja .env.example)}}"


def _healthcheck(test: list[str], start: str = "30s") -> dict[str, Any]:
    return {"test": test, "interval": "10s", "timeout": "5s", "retries": 5, "start_period": start}


def infra_service(service: InfraService) -> tuple[dict[str, Any], str | None]:
    kind = service.kind
    image = service.image
    definition: dict[str, Any] = {"image": image, "restart": "unless-stopped"}
    volume: str | None = f"{kind.value}_data"
    env = {var.name: _required(var.name) for var in INFRA_VARS[kind]}
    if kind is ServiceKind.POSTGRES:
        definition["volumes"] = [f"{volume}:{postgres_data_path(image)}"]
        definition["healthcheck"] = _healthcheck(
            ["CMD-SHELL", 'pg_isready -U "$${POSTGRES_USER}" -d "$${POSTGRES_DB}"'], "20s"
        )
    elif kind is ServiceKind.MYSQL:
        definition["volumes"] = [f"{volume}:/var/lib/mysql"]
        if "mariadb" in image:
            test = ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
        else:
            test = [
                "CMD-SHELL",
                'mysqladmin ping -h 127.0.0.1 -u root --password="$${MYSQL_ROOT_PASSWORD}" --silent',
            ]
        definition["healthcheck"] = _healthcheck(test)
    elif kind is ServiceKind.REDIS:
        definition["volumes"] = [f"{volume}:/data"]
        definition["healthcheck"] = _healthcheck(["CMD", "redis-cli", "ping"], "10s")
    elif kind is ServiceKind.RABBITMQ:
        # O nome do nó (e o diretório de dados) depende do hostname: fixá-lo preserva os dados.
        definition["hostname"] = "rabbitmq"
        definition["volumes"] = [f"{volume}:/var/lib/rabbitmq"]
        definition["healthcheck"] = _healthcheck(["CMD", "rabbitmq-diagnostics", "-q", "ping"])
    if env:
        definition["environment"] = env
    definition["networks"] = [DATA_NETWORK]
    definition["labels"] = {MANAGED_LABEL: "true"}
    return definition, volume


def connection_values(service: InfraService) -> dict[str, str]:
    """Valores que apontam para o serviço local dentro da rede do Compose."""
    kind = service.kind
    host = kind.value
    port = str(SPECS[kind].port)
    if kind is ServiceKind.POSTGRES:
        scheme = service.url_scheme or "postgresql"
        url = f"{scheme}://${{POSTGRES_USER}}:${{POSTGRES_PASSWORD}}@{host}:{port}/${{POSTGRES_DB}}"
        return {
            "url": url,
            "host": host,
            "port": port,
            "name": "${POSTGRES_DB}",
            "user": "${POSTGRES_USER}",
            "password": "${POSTGRES_PASSWORD}",
        }
    if kind is ServiceKind.MYSQL:
        scheme = service.url_scheme or "mysql"
        url = f"{scheme}://${{MYSQL_USER}}:${{MYSQL_PASSWORD}}@{host}:{port}/${{MYSQL_DATABASE}}"
        return {
            "url": url,
            "host": host,
            "port": port,
            "name": "${MYSQL_DATABASE}",
            "user": "${MYSQL_USER}",
            "password": "${MYSQL_PASSWORD}",
        }
    if kind is ServiceKind.REDIS:
        scheme = service.url_scheme or "redis"
        return {
            "url": f"{scheme}://{host}:{port}/0",
            "host": host,
            "port": port,
            "broker": f"{scheme}://{host}:{port}/0",
            "backend": f"{scheme}://{host}:{port}/1",
        }
    url = f"amqp://${{RABBITMQ_DEFAULT_USER}}:${{RABBITMQ_DEFAULT_PASS}}@{host}:{port}/"
    return {"url": url, "host": host, "port": port, "broker": url}


def app_connections(
    app: Application, services: list[InfraService]
) -> tuple[dict[str, str], list[str]]:
    """Variáveis de conexão a injetar na aplicação e avisos de configuração manual."""
    env: dict[str, str] = {}
    warnings: list[str] = []
    referenced = set(app.env_vars)
    used = [s for s in services if app.name in s.used_by]
    # Celery: se RabbitMQ local é usado, ele é o broker; Redis fica como backend de resultados.
    rabbit_used = any(s.kind is ServiceKind.RABBITMQ for s in used)
    for service in used:
        values = connection_values(service)
        injected = False
        for role, names in WIRING[service.kind].items():
            if role == "broker" and service.kind is ServiceKind.REDIS and rabbit_used:
                continue
            for name in names:
                if name in referenced and name not in env:
                    env[name] = values[role]
                    injected = True
        if not injected:
            label = SPECS[service.kind].label
            warnings.append(
                f"A aplicação '{app.name}' usa {label}, mas nenhuma variável de conexão conhecida "
                f"foi encontrada no código. Configure o host '{service.kind.value}' "
                f"(porta {SPECS[service.kind].port}) manualmente."
            )
    return env, warnings


def _app_healthcheck(app: Application, process: Process, port: int | None) -> dict[str, Any] | None:
    if process.kind is ProcessKind.STATIC:
        return _healthcheck(["CMD", "wget", "-q", "--spider", f"http://127.0.0.1:{port}/"], "10s")
    if process.kind is ProcessKind.WEB and port and process.health_path:
        return _healthcheck(_http_check(app, port, process.health_path))
    if process.kind is ProcessKind.WEB and port:
        if app.language is Language.PYTHON:
            code = f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=3)"
            return _healthcheck(["CMD", "python", "-c", code])
        code = (
            f"require('net').connect({port},'127.0.0.1')"
            ".on('connect',()=>process.exit(0)).on('error',()=>process.exit(1))"
        )
        return _healthcheck(["CMD", "node", "-e", code])
    if process.kind is ProcessKind.WORKER and process.celery_app:
        return _healthcheck(
            [
                "CMD-SHELL",
                f"celery -A {process.celery_app} inspect ping -d celery@$$HOSTNAME "
                "--timeout 10 || exit 1",
            ],
            "30s",
        )
    return None


def _http_check(app: Application, port: int, path: str) -> list[str]:
    """Requisição HTTP sem seguir redirecionamentos; qualquer status abaixo de 500 é saudável."""
    if app.language is Language.PYTHON:
        code = (
            "import http.client, sys\n"
            f"conn = http.client.HTTPConnection('127.0.0.1', {port}, timeout=3)\n"
            f"conn.request('GET', {path!r})\n"
            "sys.exit(0 if conn.getresponse().status < 500 else 1)"
        )
        return ["CMD", "python", "-c", code]
    code = (
        f"require('http').get({{host:'127.0.0.1',port:{port},path:{path!r},timeout:3000}},"
        "r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"
    )
    return ["CMD", "node", "-e", code]


def _dev_process_kind(process: Process, mode: Mode) -> ProcessKind:
    if mode is Mode.DEVELOPMENT and process.kind is ProcessKind.STATIC:
        return ProcessKind.WEB
    return process.kind


def build_compose(
    analysis: ProjectAnalysis,
    mode: Mode,
    host_ports: dict[str, int],
    app_env_files: dict[str, list[str]],
    publish_ip: str | None = None,
) -> ComposeResult:
    project = compose_project_name(analysis.project_name)
    local = analysis.local_services()
    services: dict[str, Any] = {}
    volumes: list[str] = []
    ports: list[PortBinding] = []
    images: list[str] = []
    overrides: dict[str, dict[str, str]] = {}
    warnings: list[str] = []

    for service in local:
        definition, volume = infra_service(service)
        services[service.kind.value] = definition
        if volume:
            volumes.append(volume)

    db_kinds = {ServiceKind.POSTGRES, ServiceKind.MYSQL}
    host_services = analysis.host_services()
    for service in host_services:
        spec = SPECS[service.kind]
        warnings.append(
            f"{spec.label} do host: os containers o acessam em {HOST_GATEWAY} (mesma porta do "
            ".env) e nenhum container ou volume é criado para ele. O serviço precisa aceitar conexões "
            f"da rede Docker ({HOST_ACCESS_HINTS[service.kind]})."
        )
    for app in analysis.applications:
        image = image_name(project, app)
        images.append(image)
        names = service_names(app)
        env, wiring_warnings = app_connections(app, local)
        warnings.extend(wiring_warnings)
        overrides[app.name] = env
        used = [s for s in local if app.name in s.used_by]
        needs_data = bool(used)
        uses_host = any(app.name in s.used_by for s in host_services)
        data_mounts = [f"{d.volume}:{d.path}" for d in app.data_dirs]
        for data_dir in app.data_dirs:
            volumes.append(data_dir.volume)
        if mode is Mode.DEVELOPMENT and app.build_context not in (None, app.path):
            warnings.append(
                f"'{app.name}' usa um Dockerfile com contexto '{app.build_context}': o código não "
                "é montado no container em desenvolvimento (o layout da imagem é desconhecido)."
            )
        if app.data_dirs and app.existing_dockerfile:
            warnings.append(
                f"Dockerfile existente de '{app.name}': garanta que "
                f"{', '.join(d.path for d in app.data_dirs)} exista(m) na imagem com dono igual "
                "ao usuário da aplicação; senão o volume é montado como root e a escrita falha."
            )
        build = build_section(app)
        if app.build_args and mode is Mode.PRODUCTION:
            build["args"] = {arg: f"${{{arg}:-}}" for arg in app.build_args}
        env_files = [{"path": f, "required": False} for f in app_env_files.get(app.name, [])]
        depends: dict[str, Any] = {s.kind.value: {"condition": "service_healthy"} for s in used}
        migrate_name: str | None = None
        if app.migration and any(s.kind in db_kinds for s in used):
            migrate_name = migrate_service_name(app)
            services[migrate_name] = _base_app_service(
                image, build, env_files, env, depends, needs_data, mode, app, uses_host
            ) | {"command": compose_argv(app.migration.command), "restart": "no"}
        elif app.migration:
            warnings.append(
                f"Migrações de '{app.name}' ({app.migration.tool}) detectadas, mas o banco não é "
                "um container criado pelo Dockerizer: elas não serão executadas automaticamente. "
                "Execute-as manualmente: " + " ".join(app.migration.command)
            )
        main = main_process(app)
        for process in app.processes:
            name = names[process.name]
            definition = _base_app_service(
                image, build, env_files, env, depends, needs_data, mode, app, uses_host
            )
            if data_mounts:
                definition["volumes"] = [*definition.get("volumes", []), *data_mounts]
            if migrate_name and process is main:
                definition["depends_on"] = {
                    **depends,
                    migrate_name: {"condition": "service_completed_successfully"},
                }
            command = process.command_for(mode)
            # O Dockerfile existente define o CMD de produção; em desenvolvimento, o comando com
            # reload precisa ser explícito.
            dev_override = (
                process is main
                and app.existing_dockerfile is not None
                and mode is Mode.DEVELOPMENT
                and process.dev_command is not None
            )
            if command is not None and (process is not main or dev_override):
                definition["command"] = compose_argv(command.argv)
            kind = _dev_process_kind(process, mode)
            port = process.port_for(mode)
            if kind in (ProcessKind.WEB, ProcessKind.STATIC) and port:
                host = host_ports.get(name, port)
                prefix = f"{publish_ip}:" if publish_ip else ""
                definition["ports"] = [f"{prefix}{host}:{port}"]
                ports.append(PortBinding(name, port, host))
                if app.language is Language.NODE and kind is ProcessKind.WEB:
                    definition["environment"] = {
                        "PORT": str(port),
                        **definition.get("environment", {}),
                    }
            healthcheck = _app_healthcheck(app, process.model_copy(update={"kind": kind}), port)
            if healthcheck and app.existing_dockerfile is None:
                definition["healthcheck"] = healthcheck
            services[name] = definition

    labels = {MANAGED_LABEL: "true"}
    networks: dict[str, Any] = {APP_NETWORK: {"labels": dict(labels)}}
    if local:
        networks[DATA_NETWORK] = {"internal": True, "labels": dict(labels)}
    document: dict[str, Any] = {"name": project, "services": services, "networks": networks}
    if volumes:
        document["volumes"] = {v: {"labels": dict(labels)} for v in volumes}
    body = yaml.dump(
        document, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True, width=100
    )
    content = render("compose.yaml.j2", mode_label=MODE_LABELS[mode], body=body)
    return ComposeResult(
        content=content,
        document=document,
        services=list(services),
        images=images,
        volumes=volumes,
        networks=list(networks),
        ports=ports,
        env_overrides=overrides,
        warnings=warnings,
    )


def _base_app_service(
    image: str,
    build: dict[str, Any],
    env_files: list[dict[str, Any]],
    env: dict[str, str],
    depends: dict[str, Any],
    needs_data: bool,
    mode: Mode,
    app: Application,
    uses_host: bool = False,
) -> dict[str, Any]:
    definition: dict[str, Any] = {"build": dict(build), "image": image}
    if env_files:
        definition["env_file"] = [dict(f) for f in env_files]
    if env:
        definition["environment"] = dict(env)
    if depends:
        definition["depends_on"] = dict(depends)
    if mode is Mode.DEVELOPMENT and app.build_context in (None, app.path):
        volumes = [f"./{app.path}:/app" if app.path != "." else ".:/app"]
        if app.language is Language.NODE:
            volumes.append("/app/node_modules")
        definition["volumes"] = volumes
    definition["networks"] = [APP_NETWORK, DATA_NETWORK] if needs_data else [APP_NETWORK]
    if uses_host:
        # Necessário no Linux; no Docker Desktop o nome já existe.
        definition["extra_hosts"] = [f"{HOST_GATEWAY}:host-gateway"]
    definition["restart"] = "unless-stopped"
    definition["init"] = True
    definition["security_opt"] = ["no-new-privileges:true"]
    definition["labels"] = {MANAGED_LABEL: "true"}
    return definition
