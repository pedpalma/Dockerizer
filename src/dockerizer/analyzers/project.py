"""Orquestra a análise completa de um projeto (somente leitura)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from dockerizer.analyzers.datadirs import detect_data_dirs
from dockerizer.analyzers.env import IGNORED_ENV, is_sensitive, parse_env_file
from dockerizer.analyzers.infrastructure import SPECS, InfrastructureAnalyzer
from dockerizer.analyzers.nginx import analyze_nginx
from dockerizer.analyzers.node import NodeAnalyzer
from dockerizer.analyzers.process_managers import (
    apply_process_definitions,
    find_process_definitions,
)
from dockerizer.analyzers.python import PythonAnalyzer
from dockerizer.analyzers.resolution import build_questions
from dockerizer.models.architecture import (
    Application,
    EnvVar,
    ExistingDockerFile,
    Language,
    ProjectAnalysis,
)
from dockerizer.scanner.discovery import (
    ProjectFiles,
    find_app_roots,
    find_env_files,
    find_existing_docker_files,
    find_init_scripts,
    join,
    scan_project,
)

EXAMPLE_ENV_NAMES = (".env.example", ".env.sample", ".env.template", ".env.dist", "example.env")
RESERVED_NAMES = frozenset({kind.value for kind in SPECS})


def slugify(value: str, fallback: str = "projeto") -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    return slug or fallback


def _app_name(directory: str, language: Language, roots: list[tuple[str, Language]]) -> str:
    if directory == ".":
        others = [lang for d, lang in roots if d == "."]
        if len(others) > 1 and language is Language.NODE:
            return "frontend"
        return "app"
    return slugify(PurePosixPath(directory).name, fallback="app")


def _unique(name: str, used: set[str]) -> str:
    candidate = name
    counter = 2
    while candidate in used or candidate in RESERVED_NAMES:
        candidate = f"{name}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def analyze_project(root: Path, managed_paths: Iterable[str] = ()) -> ProjectAnalysis:
    """Analisa o projeto em ``root`` sem modificá-lo nem executar nada dele.

    ``managed_paths`` são arquivos gerados anteriormente pelo Dockerizer; eles são ignorados
    como evidência (evita que a própria saída influencie a próxima análise).
    """
    root = root.resolve()
    managed = set(managed_paths)
    files = scan_project(root)
    analysis = ProjectAnalysis(root=str(root), project_name=slugify(root.name))
    if files.truncated:
        analysis.warnings.append(
            "Projeto muito grande: a varredura foi interrompida no limite de arquivos."
        )

    roots = find_app_roots(files)
    used: set[str] = set()
    for directory, language in roots:
        name = _unique(_app_name(directory, language, roots), used)
        app = _analyze_app(files, directory, language, name, analysis)
        if app is not None:
            analysis.applications.append(app)
    if not analysis.applications:
        analysis.warnings.append("Nenhuma aplicação Python ou Node.js suportada foi encontrada.")

    docker_files = find_existing_docker_files(files)
    analysis.existing_docker_files = [
        ExistingDockerFile(path=rel, kind=kind, managed=rel in managed)
        for rel, kind in docker_files
    ]
    for app in analysis.applications:
        dockerfile = join(app.path, "Dockerfile")
        if files.has(dockerfile) and dockerfile not in managed:
            app.existing_dockerfile = dockerfile
            app.evidence.append(f"Dockerfile existente em {dockerfile} será preservado")

    analysis.env_files = [rel for rel in find_env_files(files) if rel not in managed]
    analysis.init_scripts = find_init_scripts(files)
    analysis.languages = sorted({a.language for a in analysis.applications}, key=str)
    analysis.env_vars = _collect_env(files, analysis.applications, analysis.env_files)

    definitions, manager_notes = find_process_definitions(files)
    apply_process_definitions(analysis, definitions)
    analysis.notes.extend(manager_notes)

    compose_files = [rel for rel, kind in docker_files if kind == "compose" and rel not in managed]
    infra = InfrastructureAnalyzer(files, analysis.applications, analysis.env_files, compose_files)
    analysis.services = infra.analyze()
    analysis.notes.extend(infra.notes)
    detect_data_dirs(files, analysis)
    analyze_nginx(files, analysis)
    for script in analysis.init_scripts:
        analysis.notes.append(f"Script de inicialização encontrado (não executado): {script}")
    analysis.questions = build_questions(analysis)
    return analysis


def _analyze_app(
    files: ProjectFiles,
    directory: str,
    language: Language,
    name: str,
    analysis: ProjectAnalysis,
) -> Application | None:
    if language is Language.PYTHON:
        python_app = PythonAnalyzer(files, directory, name).analyze()
        if python_app is None:
            analysis.notes.append(f"{directory}: arquivos Python sem dependências reconhecidas.")
        elif files.has(join(directory, "Pipfile")) and not python_app.dependency_files:
            analysis.warnings.append(f"{directory}: Pipenv não é suportado nesta versão.")
        return python_app
    node_app, reason = NodeAnalyzer(files, directory, name).analyze()
    if reason:
        analysis.notes.append(reason)
    return node_app


def _collect_env(
    files: ProjectFiles, apps: list[Application], env_files: list[str]
) -> list[EnvVar]:
    collected: dict[str, EnvVar] = {}

    def entry(name: str) -> EnvVar:
        if name not in collected:
            collected[name] = EnvVar(name=name)
        return collected[name]

    for app in apps:
        for name in app.env_vars:
            var = entry(name)
            var.sources.append(f"código ({app.path})")
            var.apps.append(app.name)
    for rel in env_files:
        file_name = PurePosixPath(rel).name
        is_example = file_name in EXAMPLE_ENV_NAMES
        for name, value in parse_env_file(files.read(rel)).items():
            if name in IGNORED_ENV:
                continue
            var = entry(name)
            var.sources.append(rel)
            sensitive = is_sensitive(name, value)
            var.sensitive = var.sensitive or sensitive
            if is_example and not sensitive and value and var.example_value is None:
                var.example_value = value
    for var in collected.values():
        var.sensitive = var.sensitive or is_sensitive(var.name)
        if var.sensitive:
            var.example_value = None
        var.sources = sorted(set(var.sources))
        var.apps = sorted(set(var.apps))
    return sorted(collected.values(), key=lambda v: v.name)
