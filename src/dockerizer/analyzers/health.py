"""Rotas de saúde (``/health``, ``/healthz``...) usadas pelo healthcheck HTTP do Compose.

O healthcheck aceita qualquer resposta abaixo de 500: ele prova que o servidor responde HTTP
(mais que uma porta aberta) sem derrubar o container se a rota tiver um prefixo de roteador
desconhecido (404) ou exigir autenticação (401).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from dockerizer.models.architecture import Application, Language, ProcessKind
from dockerizer.scanner.discovery import ProjectFiles

HEALTH_NAMES = r"(?:health|healthz|healthcheck|health-check|ping|ready|readyz|livez|status)"
PY_ROUTE_RE = re.compile(
    rf"""@\w+(?:\.\w+)*\.(?:get|api_route|route)\(\s*["'](/(?:api/)?{HEALTH_NAMES}/?)["']"""
)
DJANGO_ROUTE_RE = re.compile(rf"""\bpath\(\s*["']((?:api/)?{HEALTH_NAMES}/?)["']""")
NODE_ROUTE_RE = re.compile(rf"""\b\w+\.(?:get|all)\(\s*["'`](/(?:api/)?{HEALTH_NAMES}/?)["'`]""")
NEXT_ROUTE_RE = re.compile(
    rf"^(?:src/)?(?:app/(api/{HEALTH_NAMES})/route\.[jt]sx?|pages/(api/{HEALTH_NAMES})\.[jt]sx?)$"
)


def _relative(rel: str, app_path: str) -> str:
    return rel if app_path == "." else rel[len(app_path) + 1 :]


def find_health_path(files: ProjectFiles, app: Application) -> str | None:
    if app.language is Language.PYTHON:
        for rel in files.under(app.path, (".py",)):
            if "/tests/" in f"/{rel}" or PurePosixPath(rel).name.startswith("test_"):
                continue
            text = files.read(rel)
            match = PY_ROUTE_RE.search(text)
            if match:
                return match.group(1)
            if PurePosixPath(rel).name == "urls.py":
                django = DJANGO_ROUTE_RE.search(text)
                if django:
                    return "/" + django.group(1)
        return None
    for rel in files.under(app.path, (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")):
        if "node_modules/" in rel:
            continue
        local = _relative(rel, app.path)
        next_match = NEXT_ROUTE_RE.match(local)
        if next_match:
            return "/" + (next_match.group(1) or next_match.group(2))
        match = NODE_ROUTE_RE.search(files.read(rel))
        if match:
            return match.group(1)
    return None


def detect_health_routes(files: ProjectFiles, apps: list[Application]) -> None:
    for app in apps:
        web = next((p for p in app.processes if p.kind is ProcessKind.WEB), None)
        if web is None:
            continue
        path = find_health_path(files, app)
        if path:
            web.health_path = path
            app.evidence.append(f"Rota de saúde {path} usada no healthcheck HTTP")
