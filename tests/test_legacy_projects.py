"""Projetos que rodam fora do Docker: systemd/supervisor/pm2, nginx, banco no host e uploads."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from dockerizer.analyzers.nginx import analyze_config
from dockerizer.analyzers.process_managers import ProcessDefinition, normalize
from dockerizer.analyzers.project import analyze_project
from dockerizer.analyzers.resolution import apply_answer
from dockerizer.cli.commands import app as cli
from dockerizer.cli.commands import generated_paths
from dockerizer.generators.plan import build_plan
from dockerizer.models.architecture import Mode, ProcessKind, ServiceDecision, ServiceKind
from dockerizer.rollback.manifest import ManifestStore
from tests.conftest import FakeDocker, tree_digest

Factory = Callable[..., Path]
runner = CliRunner()

PYPROJECT = (
    '[project]\nname = "loja"\nrequires-python = ">=3.12"\n'
    'dependencies = ["fastapi", "uvicorn", "gunicorn", "psycopg", "redis"]\n'
)
API = "from fastapi import FastAPI\napp = FastAPI()\nimport os\nos.getenv('DATABASE_URL')\n"
UNIT = """[Unit]
Description=API
[Service]
User=loja
WorkingDirectory=/srv/loja/backend
ExecStart=/srv/loja/backend/.venv/bin/gunicorn -k uvicorn.workers.UvicornWorker -w 4 \\
    -b unix:/run/loja.sock --access-logfile /var/log/loja.log --daemon --pid /run/x.pid app.main:app
"""
LEGACY = {
    "backend/pyproject.toml": PYPROJECT,
    "backend/uv.lock": "",
    "backend/app/__init__.py": "",
    "backend/app/main.py": API,
    "deploy/loja-api.service": UNIT,
    ".env": "DATABASE_URL=postgresql://loja:segredo@localhost:5432/loja\n",
}


def compose_of(plan: Any) -> dict[str, Any]:
    content = next(f.content for f in plan.files if f.path == "compose.yaml")
    return yaml.safe_load(content)  # type: ignore[no-any-return]


def files_of(plan: Any) -> dict[str, str]:
    return {f.path: f.content for f in plan.files}


# --- gerenciadores de processo ---------------------------------------------------------
def test_systemd_command_is_adapted_to_the_container(make_project: Factory) -> None:
    analysis = analyze_project(make_project(LEGACY))
    [backend] = analysis.applications
    web = backend.process("web")
    assert web is not None and web.command is not None
    assert web.command.argv == [
        "gunicorn",
        "-k",
        "uvicorn.workers.UvicornWorker",
        "-w",
        "4",
        "-b",
        "0.0.0.0:8000",
        "--access-logfile",
        "-",
        "app.main:app",
    ]
    assert web.command.source.startswith("systemd (deploy/loja-api.service")
    assert web.port == 8000
    assert any("socket Unix" in w for w in backend.warnings)


@pytest.mark.parametrize(
    ("argv", "expected", "port"),
    [
        (
            ["uvicorn", "app:app", "--host", "127.0.0.1", "--port", "9000"],
            ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"],
            9000,
        ),
        (
            ["/usr/bin/python3", "manage.py", "runserver", "127.0.0.1:8001"],
            ["python", "manage.py", "runserver", "0.0.0.0:8001"],
            8001,
        ),
        (
            ["celery", "-A", "proj", "worker", "--detach", "--pidfile=/run/c.pid"],
            ["celery", "-A", "proj", "worker"],
            None,
        ),
        (["gunicorn", "wsgi:app"], ["gunicorn", "wsgi:app", "--bind", "0.0.0.0:8000"], 8000),
        (["env", "PORT=7000", "node", "server.js"], ["node", "server.js"], 7000),
    ],
)
def test_normalize(argv: list[str], expected: list[str], port: int | None) -> None:
    definition = ProcessDefinition(manager="systemd", file="x.service", name="x", argv=argv)
    assert normalize(definition) == (expected, port)


def test_supervisor_program_becomes_worker_process(make_project: Factory) -> None:
    files = {
        **LEGACY,
        "deploy/supervisor/loja.conf": (
            "[program:poller]\ncommand=/srv/loja/backend/.venv/bin/python -m app.poller\n"
            "directory=/srv/loja/backend\n\n[program:nginx]\ncommand=/usr/sbin/nginx\n"
        ),
    }
    backend = analyze_project(make_project(files)).applications[0]
    poller = backend.process("poller")
    assert poller is not None and poller.kind is ProcessKind.WORKER
    assert poller.command is not None and poller.command.argv == ["python", "-m", "app.poller"]


def test_pm2_ecosystem_js_and_json(make_project: Factory) -> None:
    package = {
        "scripts": {"build": "next build", "start": "next start"},
        "dependencies": {"next": "15", "react": "19"},
    }
    ecosystem = """module.exports = {
  apps: [
    { name: 'site', cwd: './frontend', script: 'npm', args: 'start',
      exec_mode: 'cluster', instances: 'max', env: { PORT: 3001 } },
    { name: 'fila', cwd: './frontend', script: 'jobs/worker.js' },
  ],
};
"""
    root = make_project(
        {
            "frontend/package.json": json.dumps(package),
            "frontend/package-lock.json": "{}",
            "ecosystem.config.js": ecosystem,
        }
    )
    [site] = analyze_project(root).applications
    web = site.process("web")
    assert web is not None and web.command is not None
    assert web.command.argv == ["npm", "start"] and web.port == 3001
    worker = site.process("fila")
    assert worker is not None and worker.command is not None
    assert worker.command.argv == ["node", "jobs/worker.js"]
    assert sum("modo cluster" in w for w in site.warnings) == 1


def test_procfile_wins_over_systemd(make_project: Factory) -> None:
    files = {
        **LEGACY,
        "backend/Procfile": "web: uvicorn app.main:app --host 0.0.0.0 --port $PORT\n",
    }
    web = analyze_project(make_project(files)).applications[0].process("web")
    assert web is not None and web.command is not None
    assert web.command.source == "Procfile (web)"


# --- nginx -----------------------------------------------------------------------------
NGINX = """
upstream api { server 127.0.0.1:8000; }
server {
    listen 443 ssl;
    server_name loja.example.com;   # comentário
    client_max_body_size 50m;
    location /api/ { proxy_pass http://api; }
    location /static/ { alias /srv/loja/frontend/dist/; }
}
server { listen 80; return 301 https://$host$request_uri; }
"""


def test_nginx_routes_are_mapped_to_apps(make_project: Factory) -> None:
    analysis = analyze_project(make_project({**LEGACY, "deploy/nginx/loja.conf": NGINX}))
    tls, plain = analysis.reverse_proxies
    assert tls.tls and tls.server_names == ["loja.example.com"]
    assert tls.settings == ["client_max_body_size 50m"]
    api = tls.routes[0]
    assert (api.location, api.app, api.port) == ("/api/", "backend", 8000)
    assert tls.routes[1].kind == "estatico"
    assert plain.routes[0].kind == "redirecionamento"
    assert any("nginx" in w for w in analysis.warnings)


def test_nginx_port_change_is_reported(make_project: Factory) -> None:
    analysis = analyze_project(make_project({**LEGACY, "deploy/nginx/loja.conf": NGINX}))
    apply_answer(
        analysis, next(q for q in analysis.questions if q.id == "host:postgres"), "container"
    )
    plan = build_plan(analysis, Path(analysis.root), Mode.PRODUCTION, {"backend": 8002}, {})
    assert any("location /api/" in w and "8002" in w for w in plan.warnings)


def test_nginx_parser_ignores_hash_inside_values() -> None:
    proxies = analyze_config("n.conf", "server { location / { return 200 '#ok'; } }", [])
    assert proxies[0].routes[0].target == "return 200 #ok"


# --- banco/cache que já rodam no host --------------------------------------------------
def test_localhost_in_real_env_asks_host_or_container(make_project: Factory) -> None:
    analysis = analyze_project(make_project(LEGACY))
    question = next(q for q in analysis.questions if q.id == "host:postgres")
    assert question.default is None and question.choices == ["host", "container"]


def test_existing_compose_service_does_not_ask(make_project: Factory) -> None:
    compose = "services:\n  db:\n    image: postgres:17\n"
    analysis = analyze_project(make_project({**LEGACY, "docker-compose.yml": compose}))
    assert not any(q.id.startswith("host:") for q in analysis.questions)


def test_host_answer_uses_host_gateway_and_env_docker(make_project: Factory) -> None:
    files = {
        **LEGACY,
        ".env": "DATABASE_URL=postgresql://loja:s%40fe@127.0.0.1:5433/loja\nDB_HOST=localhost\n",
    }
    root = make_project(files)
    analysis = analyze_project(root)
    apply_answer(analysis, next(q for q in analysis.questions if q.id == "host:postgres"), "host")
    assert analysis.service(ServiceKind.POSTGRES).decision is ServiceDecision.HOST  # type: ignore[union-attr]
    plan = build_plan(analysis, root, Mode.PRODUCTION, {}, {})
    generated = files_of(plan)
    assert generated[".env.docker"].splitlines()[-2:] == [
        "DATABASE_URL=postgresql://loja:s%40fe@host.docker.internal:5433/loja",
        "DB_HOST=host.docker.internal",
    ]
    backend = compose_of(plan)["services"]["backend"]
    assert backend["extra_hosts"] == ["host.docker.internal:host-gateway"]
    assert backend["env_file"][-1] == {"path": ".env.docker", "required": False}
    assert "postgres" not in compose_of(plan)["services"]
    assert "backend-migrate" not in compose_of(plan)["services"]
    assert (root / ".env").read_text() == files[".env"]  # o .env do host não muda


def test_host_question_blocks_yes_and_answer_is_remembered(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project({**LEGACY, ".gitignore": "__pycache__/\n"})
    before = tree_digest(root)
    result = runner.invoke(cli, ["setup", str(root), "--yes"])
    assert result.exit_code == 1 and "sem confirmação" in result.output
    assert tree_digest(root) == before

    result = runner.invoke(cli, ["setup", str(root)], input="host\nnao\ns\n")
    assert result.exit_code == 0, result.output
    manifest = ManifestStore(root).load()
    assert manifest is not None
    assert manifest.answers == {"host:postgres": "host", "service:redis": "nao"}
    assert (root / ".env.docker").stat().st_mode & 0o777 == 0o600
    assert ".env.docker" in (root / ".gitignore").read_text()

    result = runner.invoke(cli, ["setup", str(root), "--yes"])
    assert result.exit_code == 0 and "Resposta registrada reutilizada" in result.output
    assert runner.invoke(cli, ["rollback", str(root), "--yes"]).exit_code == 0
    assert tree_digest(root) == before


def test_appended_env_still_counts_as_evidence(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    # RabbitMQ local faz o Dockerizer acrescentar credenciais ao .env (registro "modified").
    env = (
        "DATABASE_URL=postgresql://u:p@db.rds.amazonaws.com/loja\n"
        "AMQP_URL=amqp://app:p@rabbitmq:5672//\n"
    )
    pyproject = PYPROJECT.replace('"redis"]', '"pika"]')
    root = make_project({**LEGACY, ".env": env, "backend/pyproject.toml": pyproject})
    result = runner.invoke(cli, ["setup", str(root), "--yes"])
    assert result.exit_code == 0, result.output
    manifest = ManifestStore(root).load()
    assert manifest is not None and manifest.files[".env"].action == "modified"
    analysis = analyze_project(root, generated_paths(manifest))
    assert analysis.service(ServiceKind.POSTGRES).decision is ServiceDecision.EXTERNAL  # type: ignore[union-attr]


# --- diretórios de dados ---------------------------------------------------------------
def test_data_dirs_get_named_volumes_and_ownership(make_project: Factory) -> None:
    settings = (
        "from pydantic_settings import BaseSettings\n"
        "class Settings(BaseSettings):\n    storage_path: str = 'storage'\n"
        "    media_dir: str = 'media'\n"
    )
    files = {
        **LEGACY,
        ".env": "DATABASE_URL=postgresql://u:p@db.rds.amazonaws.com/loja\nMEDIA_DIR=/srv/media\n",
        "backend/app/settings.py": settings,
    }
    root = make_project(files)
    analysis = analyze_project(root)
    [backend] = analysis.applications
    assert [(d.path, d.volume) for d in backend.data_dirs] == [
        ("/srv/media", "backend_media_data"),
        ("/app/storage", "backend_storage_data"),
    ]
    assert any("/srv/media" in w and "não são copiados" in w for w in backend.warnings)
    plan = build_plan(analysis, root, Mode.PRODUCTION, {}, {})
    doc = compose_of(plan)
    assert {"backend_media_data", "backend_storage_data"} <= set(doc["volumes"])
    assert "backend_storage_data:/app/storage" in doc["services"]["backend"]["volumes"]
    dockerfile = files_of(plan)["backend/Dockerfile"]
    assert "chown app:app /srv/media /app/storage" in dockerfile
    assert dockerfile.index("chown app:app /srv") < dockerfile.index("USER app")
    assert set(plan.compose.volumes) >= {"backend_media_data", "backend_storage_data"}


def test_django_media_root_and_temp_dirs(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "django\ngunicorn\n",
            "manage.py": 'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "site1.settings")\n',
            "site1/__init__.py": "",
            "site1/wsgi.py": "",
            "site1/settings.py": (
                "MEDIA_ROOT = BASE_DIR / 'media'\nimport os\n"
                "UPLOAD_DIR = os.getenv('UPLOAD_DIR', '/tmp/uploads')\n"
            ),
        }
    )
    [app] = analyze_project(root).applications
    assert [d.path for d in app.data_dirs] == ["/app/media"]


# --- interface -------------------------------------------------------------------------
def test_project_text_with_brackets_is_not_swallowed(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project(LEGACY)
    result = runner.invoke(cli, ["analyze", str(root)])
    assert "[backend]" in result.output
