"""Portas em loopback, Compose existente, .env.docker desatualizado e melhorias menores."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from dockerizer.analyzers.env import is_sensitive
from dockerizer.analyzers.process_managers import effective_argv
from dockerizer.analyzers.project import analyze_project
from dockerizer.analyzers.resolution import apply_answer
from dockerizer.cli.commands import app as cli
from dockerizer.generators.plan import build_plan, previous_host_ports
from dockerizer.models.architecture import Mode, ProjectAnalysis
from tests.conftest import FASTAPI_PROJECT, FakeDocker, tree_digest

Factory = Callable[..., Path]
runner = CliRunner()
NGINX = "server { listen 80; location / { proxy_pass http://127.0.0.1:8000; } }\n"


def plan_for(analysis: ProjectAnalysis, mode: Mode = Mode.PRODUCTION, **kwargs: Any) -> Any:
    for question in list(analysis.questions):
        if question.default:
            apply_answer(analysis, question, question.default)
    return build_plan(analysis, Path(analysis.root), mode, {}, {}, secret=lambda: "x", **kwargs)


def compose_of(plan: Any) -> dict[str, Any]:
    return yaml.safe_load(next(f.content for f in plan.files if f.path == "compose.yaml"))  # type: ignore[no-any-return]


def content(plan: Any, path: str) -> str:
    return str(next(f.content for f in plan.files if f.path == path))


# --- 1. portas publicadas em loopback com proxy -----------------------------------------
def test_reverse_proxy_publishes_on_loopback(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project({**FASTAPI_PROJECT, "nginx/site.conf": NGINX})
    result = runner.invoke(cli, ["setup", str(root), "--yes"])
    assert result.exit_code == 0, result.output
    assert "127.0.0.1" in result.output
    compose = yaml.safe_load((root / "compose.yaml").read_text())
    assert compose["services"]["app"]["ports"] == ["127.0.0.1:8000:8000"]
    assert previous_host_ports(root) == {"app": 8000}


def test_host_ip_option_is_validated_and_remembered(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project({**FASTAPI_PROJECT, "nginx/site.conf": NGINX})
    result = runner.invoke(cli, ["setup", str(root), "--yes", "--host-ip", "localhost"])
    assert result.exit_code == 1 and "--host-ip inválido" in result.output
    assert runner.invoke(cli, ["setup", str(root), "--yes", "--host-ip", "0.0.0.0"]).exit_code == 0
    compose = yaml.safe_load((root / "compose.yaml").read_text())
    assert compose["services"]["app"]["ports"] == ["8000:8000"]
    assert runner.invoke(cli, ["setup", str(root), "--yes"]).exit_code == 0  # escolha mantida
    compose = yaml.safe_load((root / "compose.yaml").read_text())
    assert compose["services"]["app"]["ports"] == ["8000:8000"]


def test_without_proxy_ports_stay_on_all_interfaces(make_project: Factory) -> None:
    plan = plan_for(analyze_project(make_project(FASTAPI_PROJECT)))
    assert compose_of(plan)["services"]["app"]["ports"] == ["8000:8000"]


# --- 2. Compose existente como fonte ----------------------------------------------------
EXISTING = {
    "backend/requirements.txt": "fastapi\nuvicorn\ncelery\n",
    "backend/app/__init__.py": "",
    "backend/app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    "backend/alembic.ini": "[alembic]\n",
    "infra/docker/backend/Dockerfile": "FROM python:3.12-slim\nUSER app\n",
    "infra/docker/backend/Dockerfile.dev": "FROM python:3.12-slim\nUSER app\n",
    "frontend/package.json": json.dumps(
        {"scripts": {"build": "next build", "start": "next start"}, "dependencies": {"next": "15"}}
    ),
    "frontend/package-lock.json": "{}",
    "frontend/Dockerfile": "FROM node:22-alpine\nUSER node\n",
    "docker-compose.yml": """
services:
  api:
    build: {context: ., dockerfile: infra/docker/backend/Dockerfile.dev}
    image: loja-api:dev
    command: uvicorn app.main:app --host 0.0.0.0 --reload
  poller:
    image: loja-api:dev
    command: ["sh", "-c", "exec python -m app.poller --n=$${POLLERS:-2}"]
  cron:
    image: loja-api:dev
    command: python -m app.cron --every ${CRON_EVERY}
  migrate:
    image: loja-api:dev
    command: alembic upgrade head
  seed:
    image: loja-api:dev
    command: python -m app.seed
    restart: "no"
  front:
    build: ./frontend
    command: sh -c 'rm -rf .next/cache && exec npm run dev'
""",
    "docker-compose.prod.yml": """
services:
  api:
    build: {context: ., dockerfile: infra/docker/backend/Dockerfile, target: prod}
  devices:
    build: {context: ., dockerfile: infra/docker/backend/Dockerfile, target: prod}
    command: bash -c 'python -m app.render_config && exec python -m app.devices'
""",
}


def test_compose_dockerfile_outside_app_with_root_context(make_project: Factory) -> None:
    analysis = analyze_project(make_project(EXISTING))
    backend = analysis.app("backend")
    assert backend is not None
    assert backend.existing_dockerfile == "infra/docker/backend/Dockerfile"  # prod prevalece
    assert (backend.build_context, backend.build_target) == (".", "prod")
    plan = plan_for(analysis)
    service = compose_of(plan)["services"]["backend"]
    assert service["build"] == {
        "context": ".",
        "dockerfile": "infra/docker/backend/Dockerfile",
        "target": "prod",
    }
    poller = compose_of(plan)["services"]["backend-poller"]
    assert poller["command"][-1] == "exec python -m app.poller --n=$${POLLERS:-2}"  # reescapado
    root_ignore = content(plan, ".dockerignore")
    assert "\nfrontend\n" in root_ignore and "\nbackend\n" not in root_ignore
    assert "backend/.dockerignore" not in {f.path for f in plan.files}


def test_compose_commands_become_processes(make_project: Factory) -> None:
    analysis = analyze_project(make_project(EXISTING))
    backend = analysis.app("backend")
    assert backend is not None
    poller, devices = backend.process("poller"), backend.process("devices")
    assert poller is not None and poller.command is not None
    # "$$" do Compose é um "$" literal para o shell do container; "${VAR}" é interpolação.
    assert poller.command.argv == ["sh", "-c", "exec python -m app.poller --n=${POLLERS:-2}"]
    assert backend.process("cron") is None
    assert any("cron" in n and "variáveis" in n for n in analysis.notes)
    assert devices is not None and devices.command is not None
    assert devices.command.source == "compose (docker-compose.prod.yml: devices)"
    assert backend.migration is not None and backend.migration.command == [
        "alembic",
        "upgrade",
        "head",
    ]
    assert backend.process("seed") is None
    assert any("seed" in n and "execução única" in n for n in analysis.notes)
    web = backend.process("web")
    assert web is not None and web.command is not None
    assert "--reload" not in web.command.argv  # comando de dev não vale em produção


def test_development_mode_prefers_base_compose_and_dev_commands(make_project: Factory) -> None:
    analysis = analyze_project(make_project(EXISTING), mode=Mode.DEVELOPMENT)
    backend, frontend = analysis.app("backend"), analysis.app("frontend")
    assert backend is not None and frontend is not None
    assert backend.existing_dockerfile == "infra/docker/backend/Dockerfile.dev"
    web = frontend.process("web")
    assert web is not None and web.dev_command is not None
    assert web.dev_command.argv[:2] == ["sh", "-c"]
    assert frontend.process("front") is None
    plan = plan_for(analysis, Mode.DEVELOPMENT)
    service = compose_of(plan)["services"]["backend"]
    assert "volumes" not in service  # layout da imagem desconhecido: código não é montado
    assert service["command"][-1] == "--reload"  # CMD do Dockerfile é de produção


def test_production_ignores_dev_only_dockerfile(make_project: Factory) -> None:
    files = {k: v for k, v in EXISTING.items() if k != "docker-compose.prod.yml"}
    backend = analyze_project(make_project(files)).app("backend")
    assert backend is not None and backend.existing_dockerfile is None
    assert any("Dockerfile.dev" in w for w in backend.warnings)


def test_effective_argv_unwraps_shell() -> None:
    assert effective_argv(["sh", "-c", "a && exec uvicorn m:app"]) == ["uvicorn", "m:app"]
    assert effective_argv(["python", "x.py"]) == ["python", "x.py"]
    block = "prep > /tmp/x && \\\nexec uvicorn m:app \\\n  --port 8000\n"  # bloco YAML "|"
    assert effective_argv(["sh", "-c", block]) == ["uvicorn", "m:app", "--port", "8000"]


# --- 4. .env.docker desatualizado -------------------------------------------------------
def test_stale_env_docker_is_reported(make_project: Factory, fake_docker: FakeDocker) -> None:
    files = {
        "requirements.txt": "fastapi\nuvicorn\npsycopg\n",
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\nimport os\nos.getenv('DATABASE_URL')\n",
        ".env": "DATABASE_URL=postgresql://u:p@localhost:5432/db\n",
    }
    root = make_project(files)
    assert runner.invoke(cli, ["setup", str(root)], input="host\ns\n").exit_code == 0
    result = runner.invoke(cli, ["status", str(root)])
    assert "mudou depois" not in result.output
    (root / ".env").write_text("DATABASE_URL=postgresql://u:nova@localhost:5432/db\n")
    for command in ("status", "up"):
        result = runner.invoke(cli, [command, str(root)])
        assert ".env mudou depois da geração de .env.docker" in result.output, command


# --- melhorias menores ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ACCESS_TOKEN_EXPIRE_MINUTES", False),
        ("ADMIN_MUST_CHANGE_PASSWORD", False),
        ("JWT_ALGORITHM", False),
        ("PASSWORD_MIN_LENGTH", False),
        ("SECRET_KEY_FILE", False),
        ("SECRET_KEY", True),
        ("DB_PASSWORD", True),
        ("TELEGRAM_BOT_TOKEN", True),
    ],
)
def test_sensitive_names(name: str, expected: bool) -> None:
    assert is_sensitive(name) is expected


def test_url_with_credentials_is_always_sensitive() -> None:
    assert is_sensitive("CONNECTION_TIMEOUT", "postgresql://u:p@h/db")


def test_rollback_images_only_when_requested(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project(FASTAPI_PROJECT)
    assert runner.invoke(cli, ["setup", str(root), "--yes"]).exit_code == 0
    snapshot = tree_digest(root)
    assert (
        runner.invoke(cli, ["rollback", str(root), "--yes", "--dry-run", "--images"]).exit_code == 0
    )
    assert tree_digest(root) == snapshot
    assert ["docker", "image", "rm", "projeto-dockerizer-app"] not in fake_docker.calls
    result = runner.invoke(cli, ["rollback", str(root), "--yes", "--images"])
    assert result.exit_code == 0, result.output
    assert ["docker", "image", "rm", "projeto-dockerizer-app"] in fake_docker.calls
    assert "Imagem removida" in result.output


def test_rollback_keeps_images_by_default(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = make_project(FASTAPI_PROJECT)
    assert runner.invoke(cli, ["setup", str(root), "--yes"]).exit_code == 0
    assert runner.invoke(cli, ["rollback", str(root), "--yes"]).exit_code == 0
    assert not any(c[1:3] == ["image", "rm"] for c in fake_docker.calls)


def test_django_production_warnings(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "django\ngunicorn\n",
            "manage.py": 'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "site1.settings")\n',
            "site1/__init__.py": "",
            "site1/wsgi.py": "",
            "site1/settings.py": "DEBUG = True\nALLOWED_HOSTS = []\n",
        }
    )
    warnings = " ".join(analyze_project(root).applications[0].warnings)
    assert "DEBUG = True" in warnings and "ALLOWED_HOSTS" in warnings and "STATIC_ROOT" in warnings


def test_next_standalone_uses_server_js(make_project: Factory) -> None:
    package = {
        "scripts": {"build": "next build", "start": "next start"},
        "dependencies": {"next": "15"},
    }
    root = make_project(
        {
            "package.json": json.dumps(package),
            "package-lock.json": "{}",
            "next.config.mjs": "export default { output: 'standalone' }\n",
            "public/robots.txt": "",
        }
    )
    dockerfile = content(plan_for(analyze_project(root)), "Dockerfile")
    assert "COPY --from=build --chown=node:node /app/.next/standalone ./" in dockerfile
    assert "/app/public ./public" in dockerfile
    assert 'CMD ["node", "server.js"]' in dockerfile and "npm prune" not in dockerfile


def test_health_routes_use_http_healthcheck(make_project: Factory) -> None:
    code = "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/healthz')\ndef h(): ...\n"
    root = make_project({"requirements.txt": "fastapi\nuvicorn\n", "main.py": code})
    plan = plan_for(analyze_project(root))
    test = compose_of(plan)["services"]["app"]["healthcheck"]["test"]
    assert test[:3] == ["CMD", "python", "-c"] and "'/healthz'" in test[3]


def test_next_api_route_is_health_path(make_project: Factory) -> None:
    package = {
        "scripts": {"build": "next build", "start": "next start"},
        "dependencies": {"next": "15"},
    }
    root = make_project(
        {
            "package.json": json.dumps(package),
            "package-lock.json": "{}",
            "app/api/health/route.ts": "export function GET() {}\n",
        }
    )
    web = analyze_project(root).applications[0].process("web")
    assert web is not None and web.health_path == "/api/health"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(int(self.path.strip("/")))
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def http_server() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


@pytest.mark.parametrize(
    ("status", "healthy"), [(200, True), (404, True), (302, True), (503, False)]
)
def test_python_http_healthcheck_runs(http_server: int, status: int, healthy: bool) -> None:
    from dockerizer.generators.compose import _http_check
    from dockerizer.models.architecture import Application

    app = Application.model_validate(
        {
            "name": "a",
            "path": ".",
            "language": "python",
            "language_version": "3.12",
            "package_manager": "pip",
            "role": "backend",
        }
    )
    command = _http_check(app, http_server, f"/{status}")
    result = subprocess.run([sys.executable, "-c", command[3]], capture_output=True, check=False)
    assert (result.returncode == 0) is healthy


def test_container_replacing_host_db_prints_copy_commands(make_project: Factory) -> None:
    files = {
        "requirements.txt": "fastapi\nuvicorn\npsycopg\n",
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\nimport os\nos.getenv('DATABASE_URL')\n",
        ".env": "DATABASE_URL=postgresql://loja:segredo@localhost:5433/vendas\n",
    }
    root = make_project(files)
    analysis = analyze_project(root)
    apply_answer(
        analysis, next(q for q in analysis.questions if q.id == "host:postgres"), "container"
    )
    hints = [w for w in plan_for(analysis).warnings if "pg_dump" in w]
    assert len(hints) == 1
    assert "pg_dump -h localhost -p 5433 -U loja -Fc vendas" in hints[0]
    assert "segredo" not in hints[0]
    assert "-p projeto-dockerizer exec postgres" in hints[0]
