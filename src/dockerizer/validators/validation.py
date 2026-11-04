"""Validação estática da configuração Docker e, quando disponível, via ``docker compose config``.

A validação estática funciona sem Docker instalado e aceita conteúdos ainda não gravados
(usada no dry-run).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from dockerizer.analyzers.env import is_sensitive, parse_env_file
from dockerizer.analyzers.infrastructure import SPECS
from dockerizer.constants import COMPOSE_FILE
from dockerizer.docker.manager import DockerManager
from dockerizer.fs.paths import PathSecurityError, read_text, resolve_inside

REQUIRED_VAR_RE = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*):\?[^}]*\}")
DOCKERFILE_SECRET_RE = re.compile(
    r"^\s*(ENV|ARG)\s+([A-Za-z_][A-Za-z0-9_]*)[= ]\s*(\S+)", re.I | re.M
)
COPY_ENV_RE = re.compile(r"^\s*(?:COPY|ADD)\b[^\n]*?(?:\s|/)\.env(?:\s|$)", re.I | re.M)


class Level(StrEnum):
    ERROR = "erro"
    WARNING = "aviso"


@dataclass
class Issue:
    level: Level
    message: str
    file: str | None = None


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)
    docker_checked: bool = False

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level is Level.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level is Level.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, level: Level, message: str, file: str | None = None) -> None:
        self.issues.append(Issue(level, message, file))


class Validator:
    """Valida o projeto. ``overrides`` permite validar conteúdos ainda não escritos."""

    def __init__(
        self,
        root: Path,
        overrides: dict[str, str] | None = None,
        port_checker: Callable[[int], bool] | None = None,
        own_ports: set[int] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.overrides = overrides or {}
        self.port_checker = port_checker
        self.own_ports = own_ports or set()
        self.result = ValidationResult()
        self._checked: set[str] = set()

    def _exists(self, rel: str) -> bool:
        if rel in self.overrides:
            return True
        try:
            return resolve_inside(self.root, rel).is_file()
        except PathSecurityError:
            return False

    def _read(self, rel: str) -> str | None:
        if rel in self.overrides:
            return self.overrides[rel]
        try:
            path = resolve_inside(self.root, rel)
        except PathSecurityError:
            return None
        return read_text(path) if path.is_file() else None

    def run(self) -> ValidationResult:
        content = self._read(COMPOSE_FILE)
        if content is None:
            self.result.add(Level.ERROR, f"{COMPOSE_FILE} não encontrado.", COMPOSE_FILE)
            return self.result
        try:
            document: Any = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            self.result.add(Level.ERROR, f"YAML inválido: {exc}", COMPOSE_FILE)
            return self.result
        services = document.get("services") if isinstance(document, dict) else None
        if not isinstance(services, dict) or not services:
            self.result.add(Level.ERROR, "Nenhum serviço definido.", COMPOSE_FILE)
            return self.result
        self._services(document, services)
        self._env(content)
        return self.result

    # -- compose --------------------------------------------------------------------------
    def _services(self, document: dict[str, Any], services: dict[str, Any]) -> None:
        declared_volumes = set((document.get("volumes") or {}).keys())
        declared_networks = set((document.get("networks") or {}).keys()) | {"default"}
        host_ports: dict[int, str] = {}
        contexts: set[str] = set()
        for name, service in services.items():
            if not isinstance(service, dict):
                self.result.add(Level.ERROR, f"Serviço '{name}' inválido.", COMPOSE_FILE)
                continue
            build = service.get("build")
            if not service.get("image") and not build:
                self.result.add(Level.ERROR, f"Serviço '{name}' sem 'image' nem 'build'.")
            if isinstance(build, dict):
                self._build(name, build, contexts)
            self._depends(name, service, services)
            for volume in service.get("volumes", []) or []:
                source = str(volume).split(":", 1)[0] if ":" in str(volume) else None
                if (
                    source
                    and not source.startswith((".", "/", "~", "$"))
                    and (source not in declared_volumes)
                ):
                    self.result.add(Level.ERROR, f"Volume '{source}' de '{name}' não declarado.")
            for network in service.get("networks", []) or []:
                if network not in declared_networks:
                    self.result.add(Level.ERROR, f"Rede '{network}' de '{name}' não declarada.")
            self._ports(name, service, host_ports)
        for context in sorted(contexts):
            self._dockerignore(context)

    def _build(self, name: str, build: dict[str, Any], contexts: set[str]) -> None:
        context = str(build.get("context", "."))
        dockerfile = str(build.get("dockerfile", "Dockerfile"))
        rel = (PurePosixPath(context) / dockerfile).as_posix()
        try:
            context_path = resolve_inside(self.root, context)
        except PathSecurityError as exc:
            self.result.add(Level.ERROR, f"Contexto de build de '{name}': {exc}")
            return
        if not context_path.is_dir():
            self.result.add(Level.ERROR, f"Contexto de build de '{name}' não existe: {context}")
            return
        contexts.add(context)
        if not self._exists(rel):
            self.result.add(Level.ERROR, f"Dockerfile de '{name}' não encontrado: {rel}", rel)
            return
        if rel not in self._checked:
            self._checked.add(rel)
            self._dockerfile(rel, self._read(rel) or "")

    def _dockerfile(self, rel: str, content: str) -> None:
        if not re.search(r"^\s*FROM\s+\S+", content, re.I | re.M):
            self.result.add(Level.ERROR, "Dockerfile sem instrução FROM.", rel)
        users = re.findall(r"^\s*USER\s+(\S+)", content, re.I | re.M)
        if not users or users[-1] in ("root", "0"):
            self.result.add(Level.WARNING, "A imagem final executa como root.", rel)
        if COPY_ENV_RE.search(content):
            self.result.add(Level.ERROR, "O Dockerfile copia o arquivo .env para a imagem.", rel)
        for match in DOCKERFILE_SECRET_RE.finditer(content):
            name, value = match.group(2), match.group(3)
            if is_sensitive(name) and not value.startswith("$"):
                self.result.add(
                    Level.ERROR, f"Segredo '{name}' embutido na imagem via {match.group(1)}.", rel
                )

    def _dockerignore(self, context: str) -> None:
        rel = (PurePosixPath(context) / ".dockerignore").as_posix()
        content = self._read(rel)
        if content is None:
            self.result.add(
                Level.WARNING,
                f"Contexto '{context}' sem .dockerignore: arquivos sensíveis podem ir para a imagem.",
                rel,
            )
            return
        lines = {line.strip() for line in content.splitlines()}
        if not lines & {".env", ".env*", "*.env", "**/.env"}:
            self.result.add(Level.WARNING, f"{rel} não exclui o arquivo .env.", rel)

    def _depends(self, name: str, service: dict[str, Any], services: dict[str, Any]) -> None:
        depends = service.get("depends_on") or {}
        items = depends.items() if isinstance(depends, dict) else ((d, {}) for d in depends)
        for target, options in items:
            if target not in services:
                self.result.add(Level.ERROR, f"'{name}' depende de serviço inexistente '{target}'.")
                continue
            condition = (options or {}).get("condition") if isinstance(options, dict) else None
            if condition == "service_healthy" and not services[target].get("healthcheck"):
                self.result.add(
                    Level.ERROR,
                    f"'{name}' aguarda '{target}' saudável, mas '{target}' não tem healthcheck.",
                )

    def _ports(self, name: str, service: dict[str, Any], host_ports: dict[int, str]) -> None:
        image = str(service.get("image", ""))
        is_infra = any(spec.image_re.match(image) for spec in SPECS.values())
        for entry in service.get("ports", []) or []:
            parts = str(entry).split(":")
            if is_infra:
                self.result.add(
                    Level.WARNING,
                    f"Serviço interno '{name}' publica a porta {entry} no host.",
                )
            if len(parts) < 2 or not parts[-2].isdigit():
                continue
            host = int(parts[-2])
            if host in host_ports:
                self.result.add(
                    Level.ERROR,
                    f"Porta {host} publicada por '{host_ports[host]}' e '{name}'.",
                )
            host_ports[host] = name
            if self.port_checker and host not in self.own_ports and not self.port_checker(host):
                self.result.add(
                    Level.WARNING,
                    f"Porta {host} (serviço '{name}') já está em uso no host.",
                )

    def _env(self, compose_content: str) -> None:
        env_content = self._read(".env")
        defined = parse_env_file(env_content or "")
        required = sorted(set(REQUIRED_VAR_RE.findall(compose_content)))
        missing = [name for name in required if not defined.get(name)]
        if missing:
            self.result.add(
                Level.ERROR,
                "Variáveis obrigatórias ausentes no .env: " + ", ".join(missing),
                ".env",
            )


def validate(
    root: Path,
    overrides: dict[str, str] | None = None,
    docker: DockerManager | None = None,
    port_checker: Callable[[int], bool] | None = None,
    own_ports: set[int] | None = None,
) -> ValidationResult:
    validator = Validator(root, overrides, port_checker, own_ports)
    result = validator.run()
    if docker is not None and result.ok and not overrides:
        if docker.cli_available():
            config = docker.config()
            result.docker_checked = True
            if not config.ok:
                detail = (config.stderr or config.stdout).strip()
                result.add(Level.ERROR, f"docker compose config falhou: {detail}", COMPOSE_FILE)
        else:
            result.add(
                Level.WARNING,
                "Docker/Docker Compose não encontrado: apenas a validação estática foi feita.",
            )
    return result
