"""Informações reaproveitadas de um Compose que o projeto já tem.

Um ``docker-compose.yml`` existente é a declaração mais direta de como o projeto roda em
containers. Dele são lidos (sem executar nada):

- **Dockerfile e contexto de build** de cada aplicação, inclusive Dockerfiles fora da pasta da
  aplicação (ex.: ``infra/docker/backend/Dockerfile`` com contexto na raiz);
- **comandos** de serviços que reutilizam a imagem da aplicação (workers, pollers), que de
  outra forma não seriam detectados;
- **migrações** declaradas como serviço de execução única.

Em produção, arquivos como ``docker-compose.prod.yml`` têm prioridade; em desenvolvimento, o
arquivo base. Variáveis de interpolação (``${VAR}``) tornam o valor indefinido e ele é ignorado.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from dockerizer.analyzers.process_managers import ProcessDefinition
from dockerizer.models.architecture import Application, Migration, Mode, ProjectAnalysis
from dockerizer.scanner.discovery import ProjectFiles

PROD_RE = re.compile(r"\.(prod|production)\.", re.IGNORECASE)
DEV_RE = re.compile(r"(^|[.\-_])dev(elopment)?($|[.\-_])", re.IGNORECASE)
DEV_COMMAND_RE = re.compile(
    r"--reload\b|\bnext dev\b|\brun dev\b|\brunserver\b|\bnodemon\b|\bvite\b"
)
MIGRATION_RE = re.compile(r"\balembic\b.*\bupgrade\b|\bmanage\.py\b.*\bmigrate\b")


@dataclass
class _Build:
    service: str
    app: Application
    context: str
    dockerfile: str
    target: str | None
    file: str


def ordered_compose_files(compose_files: list[str], mode: Mode) -> list[str]:
    """Arquivos na ordem de prioridade para o modo (o primeiro que define algo prevalece)."""

    def rank(rel: str) -> tuple[int, int, str]:
        name = PurePosixPath(rel).name
        is_prod = bool(PROD_RE.search(name))
        preferred = is_prod if mode is Mode.PRODUCTION else not is_prod
        return (0 if preferred else 1, rel.count("/"), rel)

    return sorted(compose_files, key=rank)


def _clean(path: str) -> str | None:
    if not path or "${" in path or "://" in path or path.startswith(("/", "~")):
        return None
    normalized = posixpath.normpath(path)
    return None if normalized.startswith("..") else normalized


def _app_for_build(
    service: str, context: str, dockerfile: str, apps: list[Application]
) -> Application | None:
    for app in apps:
        if app.path == context:
            return app
    # Contexto acima das aplicações (ex.: raiz): o caminho do Dockerfile ou o nome do serviço
    # indicam a aplicação (infra/docker/backend/Dockerfile → backend).
    inside = [
        a for a in apps if context == "." or a.path == context or a.path.startswith(context + "/")
    ]
    dockerfile_parts = set(PurePosixPath(dockerfile).parts[:-1])
    for app in inside:
        name = PurePosixPath(app.path).name if app.path != "." else app.name
        if name in dockerfile_parts or app.name == service or name == service:
            return app
    return None


# Interpolação do Compose: "$VAR" ou "${VAR}" (mas não "$$", que é um "$" literal).
INTERPOLATION_RE = re.compile(r"\$(?:\{|[A-Za-z_])")


def _interpolated(text: str) -> bool:
    return bool(INTERPOLATION_RE.search(text.replace("$$", "")))


def _argv(command: Any) -> list[str] | None:
    """Comando como lista, com "$$" convertido no "$" que chega ao container.

    ``None`` se o valor depende de interpolação do Compose (indefinido sem o ambiente).
    """
    if isinstance(command, list) and all(isinstance(c, (str, int)) for c in command):
        raw = [str(c) for c in command]
        if any(_interpolated(a) for a in raw):
            return None
        argv = [a.replace("$$", "$") for a in raw]
    elif isinstance(command, str):
        if _interpolated(command):
            return None
        command = command.replace("$$", "$")
        try:
            argv = shlex.split(command)
        except ValueError:
            return None
        if _needs_shell(command, argv):
            return ["sh", "-c", command]
    else:
        return None
    return argv or None


def _needs_shell(command: str, argv: list[str]) -> bool:
    """Operadores de shell fora de aspas (``a && b``, ``x > y``) ou ``$VAR`` exigem ``sh -c``.

    Comandos que já são ``sh -c '...'`` são usados como estão.
    """
    if argv and PurePosixPath(argv[0]).name in ("sh", "bash"):
        return False
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return True
    return any(set(t) <= set("|&;<>") for t in tokens) or "$" in command


def import_from_compose(
    files: ProjectFiles,
    analysis: ProjectAnalysis,
    compose_files: list[str],
    managed: set[str],
    mode: Mode,
) -> list[ProcessDefinition]:
    """Ajusta Dockerfiles/contextos das aplicações e retorna os comandos encontrados."""
    builds: dict[str, _Build] = {}
    built_by: dict[tuple[str, str], Application] = {}  # (arquivo, serviço) → aplicação
    images: dict[str, Application] = {}
    services_by_file: list[tuple[str, dict[str, Any]]] = []
    for rel in ordered_compose_files(compose_files, mode):
        try:
            data: Any = yaml.safe_load(files.read(rel))
        except yaml.YAMLError:
            continue
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, dict):
            continue
        services_by_file.append((rel, services))
        base = posixpath.dirname(rel) or "."
        for name, service in services.items():
            if not isinstance(service, dict) or "build" not in service:
                continue
            build = service["build"]
            raw_context = build if isinstance(build, str) else build.get("context", ".")
            context = _clean(posixpath.join(base, str(raw_context)))
            if context is None:
                continue
            raw_dockerfile = "Dockerfile"
            target = None
            if isinstance(build, dict):
                raw_dockerfile = str(build.get("dockerfile", "Dockerfile"))
                target = str(build["target"]) if build.get("target") else None
            dockerfile = _clean(posixpath.join(context, raw_dockerfile))
            if dockerfile is None or not files.has(dockerfile):
                continue
            app = _app_for_build(str(name), context, dockerfile, analysis.applications)
            if app is None:
                continue
            image = service.get("image")
            if isinstance(image, str):
                images.setdefault(image, app)
            built_by[(rel, str(name))] = app
            builds.setdefault(app.name, _Build(str(name), app, context, dockerfile, target, rel))

    for build in builds.values():
        _adopt_dockerfile(build, managed, mode)

    definitions: list[ProcessDefinition] = []
    seen: set[tuple[str, str]] = set()
    for rel, services in services_by_file:
        for name, service in services.items():
            if not isinstance(service, dict):
                continue
            app = _service_app(service, built_by, images, str(name), rel)
            if app is None or (app.name, str(name)) in seen:
                continue
            seen.add((app.name, str(name)))
            argv = _argv(service.get("command"))
            if argv is None:
                if service.get("command") is not None:
                    analysis.notes.append(
                        f"{rel}: comando do serviço '{name}' usa variáveis ou formato não "
                        "suportado; ignorado."
                    )
                continue
            text = " ".join(argv)
            source = f"compose ({rel}: {name})"
            if MIGRATION_RE.search(text):
                if app.migration is None:
                    tool = "alembic" if "alembic" in text else "django"
                    app.migration = Migration(tool=tool, command=argv, source=source)
                continue
            if str(service.get("restart", "")).strip("'\"") == "no":
                analysis.notes.append(
                    f"{source}: serviço de execução única ({text}); não incluído. "
                    "Execute-o manualmente se necessário."
                )
                continue
            if DEV_COMMAND_RE.search(text):
                if mode is Mode.DEVELOPMENT:
                    definitions.append(
                        ProcessDefinition("compose", rel, str(name), argv, app=app.name, dev=True)
                    )
                continue  # comandos com reload não servem para produção
            definitions.append(ProcessDefinition("compose", rel, str(name), argv, app=app.name))
    return definitions


def _service_app(
    service: dict[str, Any],
    built_by: dict[tuple[str, str], Application],
    images: dict[str, Application],
    name: str,
    rel: str,
) -> Application | None:
    """Aplicação cuja imagem o serviço executa (pelo build, pela imagem ou por ``extends``)."""
    if "build" in service:
        return built_by.get((rel, name))
    image = service.get("image")
    if isinstance(image, str) and image in images:
        return images[image]
    extends = service.get("extends")
    if isinstance(extends, dict) and isinstance(extends.get("service"), str):
        return built_by.get((rel, extends["service"]))
    return None


def _adopt_dockerfile(build: _Build, managed: set[str], mode: Mode) -> None:
    app = build.app
    if build.dockerfile in managed:
        return
    name = PurePosixPath(build.dockerfile).name
    if mode is Mode.PRODUCTION and DEV_RE.search(name):
        app.warnings.append(
            f"{build.file} usa {build.dockerfile} (desenvolvimento); ele não é usado no modo "
            "produção."
        )
        return
    app.existing_dockerfile = build.dockerfile
    app.build_context = build.context
    app.build_target = build.target
    app.evidence.append(
        f"Dockerfile {build.dockerfile} (contexto '{build.context}') declarado em {build.file}"
    )
