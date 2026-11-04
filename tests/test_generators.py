"""Geração de Dockerfiles, Compose, .dockerignore e arquivos de ambiente."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from dockerizer.analyzers.project import analyze_project
from dockerizer.generators.compose import postgres_data_path
from dockerizer.generators.dockerfile import GenerationError, render_dockerfile
from dockerizer.generators.envfiles import append_to_env
from dockerizer.generators.plan import GenerationPlan, assign_ports, build_plan
from dockerizer.models.architecture import Mode, ProjectAnalysis, ServiceDecision
from tests.conftest import FASTAPI_PROJECT, NEXT_PROJECT

Factory = Callable[..., Path]


def plan_for(
    root: Path, mode: Mode = Mode.PRODUCTION, **answers: str
) -> tuple[ProjectAnalysis, GenerationPlan]:
    analysis = analyze_project(root)
    for service in analysis.services:
        if service.decision is ServiceDecision.UNDETERMINED:
            service.decision = ServiceDecision.LOCAL
    plan = build_plan(analysis, root, mode, {}, {}, secret=lambda: "segredo-teste")
    return analysis, plan


def content(plan: GenerationPlan, path: str) -> str:
    return next(f.content for f in plan.files if f.path == path)


def compose_doc(plan: GenerationPlan) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(content(plan, "compose.yaml"))
    return data


# --- Dockerfile Python ---------------------------------------------------------------------
def test_python_uv_dockerfile(make_project: Factory) -> None:
    _, plan = plan_for(make_project(FASTAPI_PROJECT))
    dockerfile = content(plan, "Dockerfile")
    assert dockerfile.startswith("# syntax=docker/dockerfile:1")
    assert "FROM python:3.12-slim" in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "uv sync --frozen --no-install-project --no-dev" in dockerfile
    # Dependências antes do código: cache de build aproveitado.
    assert dockerfile.index("uv.lock ./") < dockerfile.index("COPY --chown=app:app . .")
    assert "USER app" in dockerfile and "EXPOSE 8000" in dockerfile
    assert 'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile
    assert ".env" not in dockerfile


def test_python_requirements_dev_mode(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "flask\n",
            "requirements-dev.txt": "-r requirements.txt\npytest\n",
            "app.py": "from flask import Flask\napp = Flask(__name__)\n",
        }
    )
    _, plan = plan_for(root, Mode.DEVELOPMENT)
    dockerfile = content(plan, "Dockerfile")
    assert "COPY requirements.txt requirements.txt" in dockerfile
    assert "pip install -r requirements.txt -r requirements-dev.txt" in dockerfile
    assert '"--debug"' in dockerfile


def test_python_poetry_dockerfile(make_project: Factory) -> None:
    root = make_project(
        {
            "pyproject.toml": '[tool.poetry.dependencies]\npython = "^3.11"\nflask = "*"\n',
            "poetry.lock": "",
            "app.py": "from flask import Flask\napp = Flask(__name__)\n",
        }
    )
    dockerfile = content(plan_for(root)[1], "Dockerfile")
    assert "COPY pyproject.toml poetry.lock ./" in dockerfile
    assert "poetry install --no-root --no-ansi --only main" in dockerfile


def test_python_pyproject_inline_dependencies(make_project: Factory) -> None:
    root = make_project(
        {
            "pyproject.toml": '[project]\nname="x"\ndependencies = ["flask>=3", "gunicorn"]\n',
            "app.py": "from flask import Flask\napp = Flask(__name__)\n",
        }
    )
    dockerfile = content(plan_for(root)[1], "Dockerfile")
    assert "pip install 'flask>=3' gunicorn" in dockerfile


def test_missing_command_raises(make_project: Factory) -> None:
    analysis = analyze_project(make_project({"requirements.txt": "requests\n", "x.py": ""}))
    with pytest.raises(GenerationError):
        render_dockerfile(analysis.applications[0], Mode.PRODUCTION, set())


# --- Dockerfile Node -----------------------------------------------------------------------
def test_nextjs_multistage_production(make_project: Factory) -> None:
    dockerfile = content(plan_for(make_project(NEXT_PROJECT))[1], "Dockerfile")
    assert "FROM node:22-alpine AS deps" in dockerfile
    assert "RUN corepack enable" in dockerfile
    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "ARG NEXT_PUBLIC_API_URL" in dockerfile
    assert "RUN pnpm run build" in dockerfile and "RUN pnpm prune --prod" in dockerfile
    assert "USER node" in dockerfile
    # Em runtime, scripts rodam via npm (sem download do pnpm pelo corepack na inicialização).
    assert 'CMD ["npm", "run", "start"]' in dockerfile


def test_react_static_served_by_nginx(make_project: Factory) -> None:
    pkg = {
        "scripts": {"dev": "vite", "build": "vite build"},
        "dependencies": {"react": "18", "react-dom": "18"},
        "devDependencies": {"vite": "5"},
    }
    root = make_project({"package.json": json.dumps(pkg), "package-lock.json": "{}"})
    _, plan = plan_for(root)
    dockerfile = content(plan, "Dockerfile")
    assert "npm ci" in dockerfile
    assert "nginx-unprivileged" in dockerfile and "USER 101" in dockerfile
    assert "COPY --from=build /app/dist /usr/share/nginx/html" in dockerfile
    assert "try_files $uri $uri/ /index.html;" in dockerfile
    ignore = content(plan, ".dockerignore")
    assert "node_modules" in ignore and "dist" in ignore

    _, dev_plan = plan_for(root, Mode.DEVELOPMENT)
    dev = content(dev_plan, "Dockerfile")
    assert "nginx" not in dev and '"--host", "0.0.0.0"' in dev
    service = compose_doc(dev_plan)["services"]["app"]
    assert "./:/app" not in service["volumes"] and ".:/app" in service["volumes"]
    assert "/app/node_modules" in service["volumes"]


def test_npmrc_with_token_is_not_copied(make_project: Factory) -> None:
    files = {**NEXT_PROJECT, ".npmrc": "//registry.npmjs.org/:_authToken=abc\n"}
    _, plan = plan_for(make_project(files))
    assert ".npmrc" not in content(plan, "Dockerfile")
    assert any(".npmrc" in w for w in plan.warnings)


# --- .dockerignore ---------------------------------------------------------------------------
def test_dockerignore_excludes_secrets_and_nested_apps(make_project: Factory) -> None:
    files = {**FASTAPI_PROJECT, **{f"web/{k}": v for k, v in NEXT_PROJECT.items()}}
    _, plan = plan_for(make_project(files))
    ignore = content(plan, ".dockerignore").splitlines()
    assert {".env", ".env.*", "!.env.example", ".git", ".dockerizer", "web"} <= set(ignore)
    assert any(f.path == "web/Dockerfile" for f in plan.files)


# --- Compose -----------------------------------------------------------------------------------
def test_compose_services_networks_volumes(make_project: Factory) -> None:
    _, plan = plan_for(make_project(FASTAPI_PROJECT))
    raw = content(plan, "compose.yaml")
    assert "&id" not in raw and "*id" not in raw  # sem aliases YAML
    doc = compose_doc(plan)
    services = doc["services"]
    assert set(services) == {"postgres", "app", "app-migrate"}
    postgres = services["postgres"]
    assert "ports" not in postgres  # serviço interno não publicado
    assert postgres["networks"] == ["data"]
    assert postgres["volumes"] == ["postgres_data:/var/lib/postgresql/data"]
    assert postgres["healthcheck"]["test"][0] == "CMD-SHELL"
    assert postgres["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    assert doc["networks"]["data"] == {"internal": True}
    assert doc["volumes"] == {"postgres_data": {}}

    app = services["app"]
    assert app["ports"] == ["8000:8000"]
    assert app["restart"] == "unless-stopped"
    assert app["depends_on"] == {
        "postgres": {"condition": "service_healthy"},
        "app-migrate": {"condition": "service_completed_successfully"},
    }
    assert app["environment"]["DATABASE_URL"] == (
        "postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"
    )
    assert app["env_file"] == [{"path": ".env", "required": False}]
    assert "healthcheck" in app and app["security_opt"] == ["no-new-privileges:true"]
    migrate = services["app-migrate"]
    assert migrate["command"] == ["alembic", "upgrade", "head"] and migrate["restart"] == "no"


def test_migrations_not_run_against_external_database(make_project: Factory) -> None:
    files = {
        **FASTAPI_PROJECT,
        ".env": "DATABASE_URL=postgresql://u:p@prod.example.com/db\n",
    }
    _, plan = plan_for(make_project(files))
    services = compose_doc(plan)["services"]
    assert "postgres" not in services and "app-migrate" not in services
    assert any("Migrações" in w for w in plan.warnings)


def test_worker_shares_image_and_has_no_ports(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "fastapi\nuvicorn\ncelery\nredis\n",
            "main.py": "import os\nfrom fastapi import FastAPI\napp = FastAPI()\nos.getenv('CELERY_BROKER_URL')\n",
            "worker.py": "from celery import Celery\ncelery = Celery('w')\n",
        }
    )
    services = compose_doc(plan_for(root)[1])["services"]
    worker = services["app-worker"]
    assert worker["image"] == services["app"]["image"]
    assert "ports" not in worker
    assert worker["command"][:3] == ["celery", "-A", "worker"]
    assert services["app"]["environment"]["CELERY_BROKER_URL"] == "redis://redis:6379/0"


def test_port_conflict_assigns_next_free_port(make_project: Factory) -> None:
    analysis = analyze_project(make_project(FASTAPI_PROJECT))
    [assignment] = assign_ports(analysis, Mode.PRODUCTION, {}, is_free=lambda p: p != 8000)
    assert assignment.conflict and assignment.host_port == 8001
    [kept] = assign_ports(analysis, Mode.PRODUCTION, {"app": 9000}, is_free=lambda p: False)
    assert kept.host_port == 9000 and not kept.conflict


def test_postgres_18_uses_new_data_path() -> None:
    assert postgres_data_path("postgres:18-alpine") == "/var/lib/postgresql"
    assert postgres_data_path("postgres:17-alpine") == "/var/lib/postgresql/data"
    assert postgres_data_path("timescale/timescaledb:2.29.1-pg17") == "/var/lib/postgresql/data"
    assert postgres_data_path("pgvector/pgvector:pg18") == "/var/lib/postgresql"


# --- variáveis de ambiente -------------------------------------------------------------------
def test_env_example_has_no_secrets(make_project: Factory) -> None:
    files = {**FASTAPI_PROJECT, ".env": "SECRET_KEY=super-secreto-real\n"}
    _, plan = plan_for(make_project(files))
    example = content(plan, ".env.example")
    assert "super-secreto-real" not in example
    assert "POSTGRES_PASSWORD=\n" in example


def test_new_env_generates_secrets_and_reuses_known(make_project: Factory) -> None:
    root = make_project({k: v for k, v in FASTAPI_PROJECT.items() if k != ".env.example"})
    analysis = analyze_project(root)
    for service in analysis.services:
        service.decision = ServiceDecision.LOCAL
    plan = build_plan(
        analysis,
        root,
        Mode.PRODUCTION,
        {},
        {"POSTGRES_PASSWORD": "senha-preservada"},
        secret=lambda: "gerado",
    )
    env = content(plan, ".env")
    assert "POSTGRES_PASSWORD=senha-preservada" in env
    assert "SECRET_KEY=gerado" in env
    env_file = next(f for f in plan.files if f.path == ".env")
    assert env_file.mode == 0o600


def test_existing_env_only_gets_missing_infra_vars(make_project: Factory) -> None:
    analysis = analyze_project(make_project(FASTAPI_PROJECT))
    original = "POSTGRES_PASSWORD=minha\nAPI_TOKEN=xyz\n"
    result = append_to_env(
        original, analysis, {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": "outra"}
    )
    assert result.startswith(original)
    assert "POSTGRES_USER=app" in result and "outra" not in result
    assert append_to_env(result, analysis, {"POSTGRES_USER": "app"}) == result
