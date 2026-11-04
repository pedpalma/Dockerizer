"""Monta o conjunto de arquivos a gerar a partir da análise (sem escrever nada)."""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import SplitResult, urlsplit

import yaml

from dockerizer.analyzers.env import (
    URL_RE,
    is_host_machine,
    parse_connection_url,
    parse_env_file,
)
from dockerizer.analyzers.infrastructure import SPECS
from dockerizer.constants import (
    COMPOSE_FILE,
    COMPOSE_FILE_NAMES,
    HOST_ENV_FILE,
    TOOL_DIR,
    compose_project_name,
)
from dockerizer.fs.changes import GeneratedFile, WritePolicy
from dockerizer.fs.paths import read_text, sha256_text
from dockerizer.generators.compose import ComposeResult, build_compose, service_names
from dockerizer.generators.dockerfile import main_process, render_dockerfile, render_dockerignore
from dockerizer.generators.envfiles import (
    append_to_env,
    generate_secret,
    infra_defaults,
    render_env_example,
    render_host_env,
    render_new_env,
)
from dockerizer.models.architecture import Mode, ProcessKind, ProjectAnalysis, ServiceKind

GITIGNORE_ENTRIES = {
    ".env": {".env", "/.env", ".env*", "*.env"},
    f"{TOOL_DIR}/": {TOOL_DIR, f"{TOOL_DIR}/", f"/{TOOL_DIR}", f"/{TOOL_DIR}/"},
}


def port_is_free(port: int) -> bool:
    """Testa se a porta TCP está livre no host (sem efeitos colaterais persistentes)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


@dataclass
class PortAssignment:
    service: str
    container_port: int
    preferred: int
    host_port: int
    reused: bool = False

    @property
    def conflict(self) -> bool:
        return self.preferred != self.host_port


def previous_host_ports(root: Path) -> dict[str, int]:
    """Portas de host usadas no compose.yaml anterior (mantidas para não 'pular' de porta)."""
    compose = root / COMPOSE_FILE
    if not compose.is_file():
        return {}
    try:
        data: Any = yaml.safe_load(read_text(compose))
    except yaml.YAMLError:
        return {}
    ports: dict[str, int] = {}
    services = data.get("services", {}) if isinstance(data, dict) else {}
    for name, service in (services or {}).items():
        for entry in (service or {}).get("ports", []) or []:
            parts = str(entry).split(":")
            if len(parts) >= 2 and parts[-2].isdigit():
                ports[name] = int(parts[-2])
    return ports


def assign_ports(
    analysis: ProjectAnalysis,
    mode: Mode,
    previous: dict[str, int],
    is_free: Callable[[int], bool] = port_is_free,
) -> list[PortAssignment]:
    assignments: list[PortAssignment] = []
    taken: set[int] = set()
    for app in analysis.applications:
        names = service_names(app)
        for process in app.processes:
            kind = process.kind
            if kind not in (ProcessKind.WEB, ProcessKind.STATIC):
                continue
            port = process.port_for(mode)
            if not port:
                continue
            name = names[process.name]
            if name in previous and previous[name] not in taken:
                host = previous[name]
                taken.add(host)
                assignments.append(PortAssignment(name, port, host, host, reused=True))
                continue
            preferred = port if port >= 1024 else port + 8000
            host = preferred
            while host in taken or not is_free(host):
                host += 1
                if host > 65535:
                    host = preferred
                    break
            taken.add(host)
            assignments.append(PortAssignment(name, port, preferred, host))
    return assignments


@dataclass
class GenerationPlan:
    files: list[GeneratedFile]
    compose: ComposeResult
    infra_values: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    # Hash dos .env de onde o .env.docker foi derivado (para detectar que ficou desatualizado).
    host_env_sources: dict[str, str] = field(default_factory=dict)


def _app_env_files(analysis: ProjectAnalysis) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for app in analysis.applications:
        files = [".env"]
        if app.path != ".":
            local = f"{app.path}/.env"
            if local in analysis.env_files:
                files.append(local)
        result[app.name] = files
    return result


LOOPBACK = "127.0.0.1"


def choose_publish_ip(
    analysis: ProjectAnalysis, requested: str | None, stored: str | None
) -> tuple[str | None, str | None]:
    """Endereço em que as portas são publicadas e o motivo (para exibir).

    Com um proxy reverso no projeto, publicar em todas as interfaces deixaria a aplicação
    acessível direto pela porta, contornando TLS, limites e autenticação do nginx.
    """
    if requested is not None:
        return (None if requested in ("0.0.0.0", "*") else requested), None
    if stored is not None:
        return (None if stored in ("0.0.0.0", "*") else stored), None
    if analysis.reverse_proxies:
        return LOOPBACK, (
            "Proxy reverso encontrado: as portas são publicadas apenas em 127.0.0.1 para que a "
            "aplicação não fique acessível sem o nginx. Use --host-ip 0.0.0.0 para publicá-las "
            "em todas as interfaces."
        )
    return None, None


def _real_env_texts(root: Path, app_env_files: dict[str, list[str]]) -> dict[str, str]:
    """Conteúdo dos .env reais carregados pelo Compose (na ordem em que são carregados)."""
    texts: dict[str, str] = {}
    for files in app_env_files.values():
        for rel in files:
            path = root / rel
            if rel not in texts and path.is_file() and not path.is_symlink():
                texts[rel] = read_text(path)
    return texts


def _host_url(kind: ServiceKind, env_texts: dict[str, str]) -> SplitResult | None:
    """URL do .env que aponta o serviço para esta máquina (usuário, porta e banco)."""
    for text in env_texts.values():
        for value in parse_env_file(text).values():
            for raw in URL_RE.findall(value):
                url = parse_connection_url(str(raw))
                if url and url.service == kind.value and is_host_machine(url.host):
                    return urlsplit(str(raw))
    return None


def data_copy_hints(analysis: ProjectAnalysis, env_texts: dict[str, str]) -> list[str]:
    """Comandos (apenas exibidos, nunca executados) para copiar os dados do serviço do host."""
    project = compose_project_name(analysis.project_name)
    compose = f"docker compose -f {COMPOSE_FILE} -p {project}"
    hints = []
    for service in analysis.local_services():
        if not service.replaces_host:
            continue
        url = _host_url(service.kind, env_texts)
        port = url.port if url and url.port else SPECS[service.kind].port
        user = (url.username if url else None) or "<usuario>"
        database = (url.path.lstrip("/") if url else "") or "<banco>"
        label = SPECS[service.kind].label
        prefix = f"O {label} novo começa vazio. Para copiar os dados do {label} do host (após 'dockerizer up'):"
        if service.kind is ServiceKind.POSTGRES:
            steps = [
                f"pg_dump -h localhost -p {port} -U {user} -Fc {database} -f backup.dump",
                f"{compose} cp backup.dump postgres:/tmp/backup.dump",
                f'{compose} exec postgres sh -c \'pg_restore -U "$POSTGRES_USER" '
                '-d "$POSTGRES_DB" --no-owner /tmp/backup.dump\'',
            ]
        elif service.kind is ServiceKind.MYSQL:
            steps = [
                f"mysqldump -h 127.0.0.1 -P {port} -u {user} -p {database} > backup.sql",
                f'{compose} exec -T mysql sh -c \'mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" '
                '"$MYSQL_DATABASE"\' < backup.sql',
            ]
        elif service.kind is ServiceKind.REDIS:
            hints.append(
                f"O Redis novo começa vazio. Se ele guarda mais que cache, gere um dump com "
                f"'redis-cli -p {port} --rdb dump.rdb' e copie-o para o volume redis_data com o "
                "serviço parado."
            )
            continue
        else:
            hints.append(
                f"O {label} novo começa vazio; mensagens em filas do host não são migradas."
            )
            continue
        hints.append(prefix + "\n  " + "\n  ".join(steps))
    return hints


def _proxy_warnings(analysis: ProjectAnalysis, compose: ComposeResult) -> list[str]:
    """Rotas do nginx existente cuja porta muda no Compose."""
    published = {b.service: b.host_port for b in compose.ports}
    warnings = []
    for proxy in analysis.reverse_proxies:
        for route in proxy.routes:
            if route.app is None or route.port is None:
                continue
            host_port = published.get(route.app)
            if host_port is not None and host_port != route.port:
                warnings.append(
                    f"nginx ({proxy.path}): 'location {route.location}' aponta para a porta "
                    f"{route.port} ('{route.app}'); no Compose a aplicação fica na porta "
                    f"{host_port} do host. Ajuste o proxy_pass ao migrar."
                )
    return warnings


def _excluded_apps(analysis: ProjectAnalysis, context: str) -> list[str]:
    """Pastas de outras aplicações dentro do contexto, a excluir do build.

    Aplicações construídas a partir deste mesmo contexto (ex.: Dockerfiles em infra/docker/
    com contexto na raiz) precisam das próprias pastas e não são excluídas.
    """
    users = {a.path for a in analysis.applications if (a.build_context or a.path) == context}
    prefix = "" if context == "." else context + "/"
    return [nested for nested in _nested_apps(analysis, context) if prefix + nested not in users]


def _nested_apps(analysis: ProjectAnalysis, app_path: str) -> list[str]:
    nested = []
    for other in analysis.applications:
        if other.path == app_path or other.path == ".":
            continue
        if app_path == ".":
            nested.append(other.path)
        elif other.path.startswith(app_path + "/"):
            nested.append(other.path[len(app_path) + 1 :])
    return sorted(set(nested))


def _safe_install_files(root: Path, analysis: ProjectAnalysis) -> tuple[set[str], list[str]]:
    """Arquivos auxiliares de instalação Node que podem ir para o build (sem tokens)."""
    allowed: set[str] = set()
    warnings: list[str] = []
    for app in analysis.applications:
        for extra in (".npmrc", ".yarnrc.yml", ".yarnrc", "pnpm-workspace.yaml"):
            rel = extra if app.path == "." else f"{app.path}/{extra}"
            path = root / rel
            if not path.is_file() or path.is_symlink():
                continue
            if "_auth" in read_text(path) or "npmAuthToken" in read_text(path):
                warnings.append(
                    f"{rel} contém credenciais e não será copiado para a imagem; configure o "
                    "acesso ao registro privado com build secrets."
                )
                continue
            allowed.add(rel)
    return allowed, warnings


def _gitignore(root: Path, extra: dict[str, set[str]] | None = None) -> GeneratedFile | None:
    path = root / ".gitignore"
    if not path.is_file() and not (root / ".git").exists():
        return None
    existing = read_text(path) if path.is_file() else ""
    lines = {line.strip() for line in existing.splitlines()}
    entries = {**GITIGNORE_ENTRIES, **(extra or {})}
    missing = [entry for entry, accepted in entries.items() if not (lines & accepted)]
    if not missing:
        return None
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    block = "\n# Dockerizer: segredos locais e estado da ferramenta\n" + "\n".join(missing) + "\n"
    return GeneratedFile(
        path=".gitignore",
        content=(prefix + block) if existing else block.lstrip("\n"),
        policy=WritePolicy.APPEND,
        description="Ignora .env e .dockerizer/ no Git",
    )


def build_plan(
    analysis: ProjectAnalysis,
    root: Path,
    mode: Mode,
    host_ports: dict[str, int],
    known_credentials: dict[str, str],
    secret: Callable[[], str] = generate_secret,
    publish_ip: str | None = None,
) -> GenerationPlan:
    """Gera o conteúdo de todos os arquivos. Lança ``GenerationError`` se faltar informação."""
    root = root.resolve()
    files: list[GeneratedFile] = []
    install_files, warnings = _safe_install_files(root, analysis)
    for app in analysis.applications:
        base = PurePosixPath(app.path)
        if app.existing_dockerfile is None:
            files.append(
                GeneratedFile(
                    path=(base / "Dockerfile").as_posix(),
                    content=render_dockerfile(app, mode, install_files),
                    policy=WritePolicy.REPLACE,
                    description=f"Imagem da aplicação '{app.name}'",
                )
            )
        else:
            process = main_process(app)
            if process and process.kind is ProcessKind.WEB:
                warnings.append(
                    f"Dockerfile existente de '{app.name}' será usado sem alterações; "
                    "confirme que ele expõe a porta esperada."
                )
        context = app.build_context or app.path
        ignore_path = (PurePosixPath(context) / ".dockerignore").as_posix()
        if ignore_path not in {f.path for f in files}:
            files.append(
                GeneratedFile(
                    path=ignore_path,
                    content=render_dockerignore(app, _excluded_apps(analysis, context)),
                    policy=WritePolicy.PRESERVE,
                    description=f"Contexto de build de '{app.name}' (exclui segredos)",
                )
            )

    app_env_files = _app_env_files(analysis)
    env_texts = _real_env_texts(root, app_env_files)
    host_env, host_warnings = render_host_env(analysis, env_texts)
    warnings.extend(host_warnings)
    if host_env is not None:
        host_users = {name for s in analysis.host_services() for name in s.used_by}
        for app_name in host_users & set(app_env_files):
            app_env_files[app_name].append(HOST_ENV_FILE)
    compose = build_compose(analysis, mode, host_ports, app_env_files, publish_ip)
    warnings.extend(compose.warnings)
    warnings.extend(_proxy_warnings(analysis, compose))
    warnings.extend(data_copy_hints(analysis, env_texts))
    files.append(
        GeneratedFile(
            path=COMPOSE_FILE,
            content=compose.content,
            policy=WritePolicy.REPLACE,
            description="Serviços, redes, volumes e healthchecks",
            shadows=tuple(
                sorted(
                    f.path
                    for f in analysis.existing_docker_files
                    if f.kind == "compose"
                    and not f.managed
                    and "/" not in f.path
                    and f.path in COMPOSE_FILE_NAMES
                    and f.path != COMPOSE_FILE
                )
            ),
        )
    )
    files.append(
        GeneratedFile(
            path=".env.example",
            content=render_env_example(analysis, compose.env_overrides),
            policy=WritePolicy.PRESERVE,
            description="Documentação das variáveis de ambiente (sem segredos)",
        )
    )

    env_path = root / ".env"
    existing_env = read_text(env_path) if env_path.is_file() else None
    known = {**known_credentials, **parse_env_file(existing_env or "")}
    infra_values = infra_defaults(analysis, known, secret)
    if existing_env is None:
        env_content = render_new_env(analysis, compose.env_overrides, infra_values, secret)
    else:
        env_content = append_to_env(existing_env, analysis, infra_values)
    files.append(
        GeneratedFile(
            path=".env",
            content=env_content,
            policy=WritePolicy.APPEND,
            description="Variáveis locais e credenciais geradas (não versionar)",
            mode=0o600,
            sensitive=True,
        )
    )
    if host_env is not None:
        files.append(
            GeneratedFile(
                path=HOST_ENV_FILE,
                content=host_env,
                policy=WritePolicy.REPLACE,
                description="Variáveis do .env apontando para os serviços do host",
                mode=0o600,
                sensitive=True,
            )
        )
    extra_ignores = {
        HOST_ENV_FILE: {HOST_ENV_FILE, f"/{HOST_ENV_FILE}", ".env*", ".env.*", "*.env*"}
    }
    gitignore = _gitignore(root, extra_ignores if host_env is not None else None)
    if gitignore:
        files.append(gitignore)
    sources = (
        {rel: sha256_text(text) for rel, text in env_texts.items()} if host_env is not None else {}
    )
    return GenerationPlan(
        files=files,
        compose=compose,
        infra_values=infra_values,
        warnings=warnings,
        host_env_sources=sources,
    )


def stale_host_env(root: Path, sources: dict[str, str]) -> list[str]:
    """Arquivos .env alterados (ou removidos) depois da geração do .env.docker."""
    stale = []
    for rel, digest in sources.items():
        path = root / rel
        current = sha256_text(read_text(path)) if path.is_file() else None
        if current != digest:
            stale.append(rel)
    return stale
