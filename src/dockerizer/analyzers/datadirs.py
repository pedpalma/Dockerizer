"""Diretórios em que a aplicação grava arquivos (uploads, mídia, storage).

Dentro de um container, tudo que é gravado fora de um volume some quando o container é
recriado. Variáveis como ``STORAGE_PATH``, ``MEDIA_ROOT`` ou ``UPLOAD_DIR`` indicam esses
diretórios; cada um recebe um volume nomeado (preservado por ``down`` e ``rollback``).

O valor vem, em ordem: ``.env`` real, ``.env.example``, padrão literal no código.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath

from dockerizer.analyzers.env import parse_env_file, python_env_defaults
from dockerizer.models.architecture import (
    Application,
    DataDir,
    Language,
    ProcessKind,
    ProjectAnalysis,
)
from dockerizer.scanner.discovery import ProjectFiles

DATA_VAR_RE = re.compile(
    r"^(?:[A-Z0-9]+_)*?(?:STORAGE|MEDIA|UPLOADS?|FILES?|ATTACHMENTS?|DATA|DOCUMENTS?|IMAGES?)"
    r"_(?:PATH|DIR|DIRECTORY|ROOT|FOLDER)$|^(?:MEDIA_ROOT|UPLOAD_FOLDER)$"
)
EXCLUDED_VAR_RE = re.compile(r"TMP|TEMP|CACHE|LOG|STATIC|CERT|BUNDLE|SOCKET|CONFIG|SCHEMA")
NODE_DEFAULT_RE = re.compile(
    r"process\.env\.([A-Z_][A-Z0-9_]*)\s*(?:\|\||\?\?)\s*['\"`]([^'\"`]+)['\"`]"
)
DJANGO_MEDIA_RE = re.compile(
    r"^MEDIA_ROOT\s*=\s*(?:BASE_DIR\s*/\s*['\"]([^'\"]+)['\"]"
    r"|os\.path\.join\(\s*BASE_DIR\s*,\s*['\"]([^'\"]+)['\"])",
    re.MULTILINE,
)
FLASK_UPLOAD_RE = re.compile(r"\[\s*['\"]UPLOAD_FOLDER['\"]\s*\]\s*=\s*['\"]([^'\"]+)['\"]")
REAL_ENV = (".env", ".env.local", ".env.production", ".env.prod")
APP_ROOT = "/app"
# Caminhos temporários ou do sistema: não são dados da aplicação.
VOLATILE_PREFIXES = ("/tmp", "/var/tmp", "/dev", "/proc")  # noqa: S108


def _container_path(value: str) -> str | None:
    value = value.strip().strip("\"'")
    if not value or "://" in value or any(c in value for c in "${}"):
        return None
    if value.startswith("/"):
        path = posixpath.normpath(value)
    else:
        relative = posixpath.normpath(value)
        if relative.startswith("..") or relative == ".":
            return None
        path = posixpath.join(APP_ROOT, relative)
    if path in ("/", APP_ROOT) or path.startswith(VOLATILE_PREFIXES):
        return None
    return path


def _volume_name(app: Application, path: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", f"{app.name}_{PurePosixPath(path).name}".lower()).strip("_")
    name, counter = f"{base}_data" if not base.endswith("_data") else base, 2
    while name in used:
        name = f"{base}_{counter}_data"
        counter += 1
    used.add(name)
    return name


def _env_values(files: ProjectFiles, env_files: list[str], app: Application) -> dict[str, str]:
    """Valores das variáveis para a aplicação: arquivos reais prevalecem sobre exemplos."""
    real: dict[str, str] = {}
    example: dict[str, str] = {}
    for rel in env_files:
        directory = posixpath.dirname(rel) or "."
        if directory not in (".", app.path):
            continue
        target = real if PurePosixPath(rel).name in REAL_ENV else example
        for name, value in parse_env_file(files.read(rel)).items():
            if value:
                target.setdefault(name, value)
    return {**example, **real}


def _code_defaults(files: ProjectFiles, app: Application) -> dict[str, tuple[str, str]]:
    """Padrões literais no código: variável → (valor, arquivo)."""
    found: dict[str, tuple[str, str]] = {}
    if app.language is Language.PYTHON:
        for rel in files.under(app.path, (".py",)):
            text = files.read(rel)
            for name, value in python_env_defaults(text).items():
                found.setdefault(name, (value, rel))
            for match in DJANGO_MEDIA_RE.finditer(text):
                found.setdefault("MEDIA_ROOT", (match.group(1) or match.group(2), rel))
            for match in FLASK_UPLOAD_RE.finditer(text):
                found.setdefault("UPLOAD_FOLDER", (match.group(1), rel))
    else:
        for rel in files.under(app.path, (".js", ".mjs", ".cjs", ".ts")):
            if "node_modules/" in rel:
                continue
            for name, value in NODE_DEFAULT_RE.findall(files.read(rel)):
                found.setdefault(name, (value, rel))
    return found


def detect_data_dirs(files: ProjectFiles, analysis: ProjectAnalysis) -> None:
    used: set[str] = set()
    for app in analysis.applications:
        if any(p.kind is ProcessKind.STATIC for p in app.processes):
            continue  # frontend estático não grava arquivos
        env = _env_values(files, analysis.env_files, app)
        defaults = _code_defaults(files, app)
        names = set(app.env_vars) | {n for n in env if n in app.env_vars or n in defaults}
        names |= {n for n in defaults if n in ("MEDIA_ROOT", "UPLOAD_FOLDER")}
        seen_paths: set[str] = set()
        for name in sorted(names):
            if not DATA_VAR_RE.match(name) or EXCLUDED_VAR_RE.search(name):
                continue
            if name in env:
                value, source = env[name], "arquivo .env"
            elif name in defaults:
                value, source = defaults[name][0], defaults[name][1]
            else:
                app.warnings.append(
                    f"{name} parece um diretório de dados, mas o valor não foi encontrado; "
                    "monte um volume para ele manualmente se a aplicação gravar arquivos."
                )
                continue
            path = _container_path(value)
            if path is None:
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)
            app.data_dirs.append(
                DataDir(
                    path=path,
                    source=f"{name}={value} ({source})",
                    variable=name,
                    volume=_volume_name(app, path, used),
                )
            )
            if value.startswith("/") and not path.startswith(APP_ROOT + "/"):
                # /app/... já é caminho de container (convenção de Dockerfiles existentes).
                app.warnings.append(
                    f"{name} aponta para {value}: arquivos que já existem nesse caminho no host "
                    "não são copiados para o volume."
                )
