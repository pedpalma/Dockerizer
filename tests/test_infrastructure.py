"""Identificação de serviços: dependência sozinha não cria serviço; configuração decide."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dockerizer.analyzers.project import analyze_project
from dockerizer.analyzers.resolution import apply_answer
from dockerizer.models.architecture import ServiceDecision, ServiceKind

Factory = Callable[..., Path]
FASTAPI = "from fastapi import FastAPI\napp = FastAPI()\n"


def _service(root: Path, kind: ServiceKind):  # type: ignore[no-untyped-def]
    service = analyze_project(root).service(kind)
    assert service is not None
    return service


def test_dependency_alone_is_undetermined_and_defaults_to_no(make_project: Factory) -> None:
    root = make_project({"requirements.txt": "fastapi\nuvicorn\nredis\n", "main.py": FASTAPI})
    analysis = analyze_project(root)
    service = analysis.service(ServiceKind.REDIS)
    assert service is not None and service.decision is ServiceDecision.UNDETERMINED
    question = next(q for q in analysis.questions if q.id == "service:redis")
    assert question.default == "nao"
    assert analysis.local_services() == []


def test_dependency_and_env_reference_suggests_local(make_project: Factory) -> None:
    code = FASTAPI + "import os\nURL = os.getenv('REDIS_URL')\n"
    root = make_project({"requirements.txt": "fastapi\nuvicorn\nredis\n", "main.py": code})
    analysis = analyze_project(root)
    question = next(q for q in analysis.questions if q.id == "service:redis")
    assert question.default == "sim"
    apply_answer(analysis, question, "sim")
    assert [s.kind for s in analysis.local_services()] == [ServiceKind.REDIS]


def test_local_url_in_env_example_means_local(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "fastapi\nuvicorn\nasyncpg\n",
            "main.py": FASTAPI,
            ".env.example": "DATABASE_URL=postgresql+asyncpg://u:p@localhost:5432/db\n",
        }
    )
    service = _service(root, ServiceKind.POSTGRES)
    assert service.decision is ServiceDecision.LOCAL
    assert service.url_scheme == "postgresql+asyncpg"


def test_real_env_external_host_wins_over_example(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "fastapi\nuvicorn\npsycopg\n",
            "main.py": FASTAPI,
            ".env.example": "DATABASE_URL=postgresql://u:p@localhost/db\n",
            ".env": "DATABASE_URL=postgresql://u:p@db.abc.us-east-1.rds.amazonaws.com/db\n",
        }
    )
    service = _service(root, ServiceKind.POSTGRES)
    assert service.decision is ServiceDecision.EXTERNAL


def test_conflicting_strong_evidence_is_undetermined(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "fastapi\nuvicorn\nredis\n",
            "main.py": FASTAPI,
            ".env": "REDIS_URL=redis://localhost:6379\nCACHE_URL=redis://cache.example.com:6379\n",
        }
    )
    assert _service(root, ServiceKind.REDIS).decision is ServiceDecision.UNDETERMINED


def test_existing_compose_service_is_local_and_image_reused(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "django\npsycopg2\n",
            "manage.py": "",
            "docker-compose.yml": "services:\n  db:\n    image: postgres:16-alpine\n",
        }
    )
    service = _service(root, ServiceKind.POSTGRES)
    assert service.decision is ServiceDecision.LOCAL
    assert service.image == "postgres:16-alpine"


def test_managed_files_are_not_evidence(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "fastapi\nuvicorn\nredis\n",
            "main.py": FASTAPI,
            "compose.yaml": "services:\n  redis:\n    image: redis:7-alpine\n",
        }
    )
    assert _service(root, ServiceKind.REDIS).decision is ServiceDecision.LOCAL
    service = analyze_project(root, managed_paths=["compose.yaml"]).service(ServiceKind.REDIS)
    assert service is not None and service.decision is ServiceDecision.UNDETERMINED


def test_rabbitmq_from_celery_broker_url(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "celery\n",
            "worker.py": "from celery import Celery\napp = Celery('w', broker='amqp://guest@localhost//')\n",
        }
    )
    assert _service(root, ServiceKind.RABBITMQ).decision is ServiceDecision.LOCAL


def test_celery_without_broker_asks_which(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "celery\n",
            "worker.py": "from celery import Celery\napp = Celery('w')\n",
        }
    )
    analysis = analyze_project(root)
    question = next(q for q in analysis.questions if q.id.startswith("broker:"))
    assert question.default is None
    apply_answer(analysis, question, "redis")
    assert [s.kind for s in analysis.local_services()] == [ServiceKind.REDIS]


def test_django_sqlite_creates_no_service(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "django\n",
            "manage.py": 'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "site1.settings")\n',
            "site1/settings.py": "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3'}}\n",
        }
    )
    analysis = analyze_project(root)
    assert analysis.services == []
    assert any("SQLite" in n for n in analysis.notes)


def test_ci_files_are_not_runtime_evidence(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "fastapi\nuvicorn\npsycopg\n",
            "main.py": FASTAPI,
            ".github/workflows/ci.yml": "env:\n  DATABASE_URL: postgresql://p:p@localhost/test\n",
        }
    )
    assert _service(root, ServiceKind.POSTGRES).decision is ServiceDecision.UNDETERMINED
