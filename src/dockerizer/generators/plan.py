"""Monta o conjunto de arquivos a gerar a partir da análise (sem escrever nada)."""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from dockerizer.analyzers.env import parse_env_file
from dockerizer.constants import COMPOSE_FILE, TOOL_DIR
from dockerizer.fs.changes import GeneratedFile, WritePolicy
from dockerizer.fs.paths import read_text
from dockerizer.generators.compose import ComposeResult, build_compose, service_names
from dockerizer.generators.dockerfile import main_process, render_dockerfile, render_dockerignore
from dockerizer.generators.envfiles import (
    append_to_env,
    generate_secret,
    infra_defaults,
    render_env_example,
    render_new_env,
)
from dockerizer.models.architecture import Mode, ProcessKind, ProjectAnalysis

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


def _gitignore(root: Path) -> GeneratedFile | None:
    path = root / ".gitignore"
    if not path.is_file() and not (root / ".git").exists():
        return None
    existing = read_text(path) if path.is_file() else ""
    lines = {line.strip() for line in existing.splitlines()}
    missing = [entry for entry, accepted in GITIGNORE_ENTRIES.items() if not (lines & accepted)]
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
        files.append(
            GeneratedFile(
                path=(base / ".dockerignore").as_posix(),
                content=render_dockerignore(app, _nested_apps(analysis, app.path)),
                policy=WritePolicy.PRESERVE,
                description=f"Contexto de build de '{app.name}' (exclui segredos)",
            )
        )

    compose = build_compose(analysis, mode, host_ports, _app_env_files(analysis))
    warnings.extend(compose.warnings)
    files.append(
        GeneratedFile(
            path=COMPOSE_FILE,
            content=compose.content,
            policy=WritePolicy.REPLACE,
            description="Serviços, redes, volumes e healthchecks",
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
    gitignore = _gitignore(root)
    if gitignore:
        files.append(gitignore)
    return GenerationPlan(
        files=files, compose=compose, infra_values=infra_values, warnings=warnings
    )
