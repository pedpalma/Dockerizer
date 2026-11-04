"""Ciclos completos com o Docker real: o que os testes simulados não conseguem provar.

Cada cenário termina com ``assert_clean``: nenhum container, volume, rede ou imagem do projeto
restante, e o projeto byte a byte igual ao original.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tests.integration.conftest import Project, _docker

pytestmark = pytest.mark.docker

API = """import os

import psycopg
import redis
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/visit")
def visit() -> dict[str, int]:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("create table if not exists visitas (id serial primary key)")
        conn.execute("insert into visitas default values")
        total = conn.execute("select count(*) from visitas").fetchone()[0]
    hits = redis.Redis.from_url(os.environ["REDIS_URL"]).incr("hits")
    return {"visitas": total, "hits": int(hits)}
"""

FASTAPI_STACK = {
    "requirements.txt": "fastapi\nuvicorn\npsycopg[binary]\nredis\n",
    "app/__init__.py": "",
    "app/main.py": API,
    ".env.example": (
        "DATABASE_URL=postgresql://app:troque@localhost:5432/loja\n"
        "REDIS_URL=redis://localhost:6379/0\n"
    ),
}


def test_fastapi_postgres_redis_full_lifecycle(project: Project) -> None:
    """setup → dados → down → rollback → setup de novo (mesma senha, dados mantidos) → purge."""
    project.write(FASTAPI_STACK)
    project.cli("setup", "--yes", "--build", "--start")
    services = project.wait_healthy("app", "postgres", "redis")
    for internal in ("postgres", "redis"):  # infraestrutura nunca publicada no host
        assert not any(p.get("PublishedPort") for p in services[internal].get("Publishers") or [])
    status, body = project.get("app", "/visit")
    assert status == 200 and json.loads(body) == {"visitas": 1, "hits": 1}

    output = project.cli("down")
    assert "preservados" in output
    assert len(project.volumes()) == 2 and project.containers() == []

    project.cli("rollback", "--yes")
    assert (project.root / ".dockerizer" / "credentials.env").is_file()  # volumes existem

    project.cli("setup", "--yes", "--build", "--start")
    project.wait_healthy("app", "postgres", "redis")
    _, body = project.get("app", "/visit")
    assert json.loads(body) == {"visitas": 2, "hits": 2}  # mesma senha, dados preservados

    project.cli("down")
    project.cli("purge", input="errado\n", expect=1)
    assert len(project.volumes()) == 2
    project.cli("purge", input=f"{project.compose_project}\n")
    assert project.volumes() == []
    project.cli("rollback", "--yes", "--images")
    project.assert_clean()


EXISTING_COMPOSE = {
    "backend/requirements.txt": "fastapi\nuvicorn\n",
    "backend/app/__init__.py": "",
    "backend/app/main.py": (
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        '@app.get("/health")\ndef health() -> dict[str, str]:\n    return {"status": "ok"}\n'
    ),
    "backend/app/poller.py": (
        "import sys\nimport time\n\nwhile True:\n"
        "    print('poll', sys.argv[1:], flush=True)\n    time.sleep(2)\n"
    ),
    "infra/docker/backend/Dockerfile": (
        "FROM python:3.12-slim\nWORKDIR /srv\nCOPY backend/requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\nCOPY backend/ .\n"
        "RUN useradd -u 10001 app\nUSER app\n"
        'CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]\n'
    ),
    "docker-compose.yml": (
        "services:\n"
        "  api:\n"
        "    build: {context: ., dockerfile: infra/docker/backend/Dockerfile}\n"
        "    image: loja-api:local\n"
        "  poller:\n"
        "    image: loja-api:local\n"
        '    command: ["sh", "-c", "exec python -m app.poller --n=$${POLLERS:-2}"]\n'
    ),
    "deploy/nginx/site.conf": (
        "server {\n  listen 80;\n  location / { proxy_pass http://127.0.0.1:8000; }\n}\n"
    ),
    ".env": "SEGREDO=nao-pode-ir-para-a-imagem\n",
}


def test_existing_compose_root_context_and_loopback(project: Project) -> None:
    """Dockerfile em infra/ com contexto na raiz, worker só no Compose e nginx na frente."""
    project.write(EXISTING_COMPOSE)
    project.cli("setup", "--yes", "--force", "--build", "--start")
    services = project.wait_healthy("backend", "backend-poller")
    assert project.published_ip("backend") == "127.0.0.1"  # nginx: nada exposto direto
    assert all(p.get("URL") in ("127.0.0.1", None) for p in services["backend"]["Publishers"])
    status, _ = project.get("backend", "/health")
    assert status == 200

    logs = _docker("compose", "-p", project.compose_project, "logs", "backend-poller").stdout
    assert "--n=2" in logs  # "$$" do Compose chegou como "$" ao shell do container

    image = f"{project.compose_project}-backend"
    listing = _docker("run", "--rm", "--entrypoint", "ls", image, "-a", "/srv").stdout.split()
    assert ".env" not in listing and "app" in listing  # .dockerignore da raiz funcionou

    # O compose do próprio projeto nunca foi tocado nem iniciado.
    assert (
        _docker(
            "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={project.root.name}"
        ).stdout
        == ""
    )
    project.cli("rollback", "--yes", "--images")
    project.assert_clean()


HOST_DB_API = """import os

import psycopg
from fastapi import FastAPI

app = FastAPI()


@app.get("/pedidos")
def pedidos() -> dict[str, int]:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        return {"pedidos": conn.execute("select count(*) from pedidos").fetchone()[0]}
"""


def test_existing_host_database_via_host_gateway(
    project: Project, host_postgres: Callable[[str, str, str], int]
) -> None:
    """Banco que já roda na máquina: acessado via host.docker.internal, sem container novo."""
    port = host_postgres("loja", "s3nh%forte", "loja")
    project.write(
        {
            "requirements.txt": "fastapi\nuvicorn\npsycopg[binary]\n",
            "app/__init__.py": "",
            "app/main.py": HOST_DB_API,
            ".env": f"DATABASE_URL=postgresql://loja:s3nh%25forte@localhost:{port}/loja\n",
            ".gitignore": "__pycache__/\n",
        }
    )
    output = project.cli("setup", "--build", "--start", input="host\n" + "s\n" * 6)
    assert "host.docker.internal" in output
    project.wait_healthy("app")
    assert "postgres" not in project.services()
    _, body = project.get("app", "/pedidos")
    assert json.loads(body) == {"pedidos": 3}  # dados reais do banco "do host"

    output = project.cli("setup", "--yes", "--start")  # resposta reaproveitada
    assert "Resposta registrada reutilizada" in output
    project.cli("rollback", "--yes", "--images")
    project.assert_clean()


NEXT_APP = {
    "package.json": json.dumps(
        {
            "name": "site",
            "private": True,
            "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
            "dependencies": {"next": "15.5.4", "react": "19.1.1", "react-dom": "19.1.1"},
        }
    ),
    "next.config.mjs": "export default { output: 'standalone' };\n",
    "app/layout.js": (
        "export default function RootLayout({ children }) {\n"
        "  return <html><body>{children}</body></html>;\n}\n"
    ),
    "app/page.js": "export default function Page() {\n  return <h1>loja ok</h1>;\n}\n",
    "app/api/health/route.js": "export function GET() {\n  return Response.json({ ok: true });\n}\n",
    "public/robots.txt": "User-agent: *\n",
}


def test_next_standalone_image(project: Project) -> None:
    """Next.js standalone: server.js, estáticos de public/ e healthcheck HTTP."""
    project.write(NEXT_APP)
    project.cli("setup", "--yes", "--build", "--start")
    services = project.wait_healthy("app", timeout=300)
    assert services["app"]["Health"] == "healthy"  # healthcheck HTTP em /api/health
    status, body = project.get("app", "/")
    assert status == 200 and "loja ok" in body
    assert project.get("app", "/robots.txt") == (200, "User-agent: *\n")
    image = f"{project.compose_project}-app"
    listing = _docker("run", "--rm", "--entrypoint", "ls", image, "/app").stdout.split()
    assert "server.js" in listing and "app" not in listing  # só a saída standalone, sem o código
    project.cli("rollback", "--yes", "--images")
    project.assert_clean()
