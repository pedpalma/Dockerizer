"""Detecção de projetos Python e Node.js, descoberta de arquivos e perguntas."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from dockerizer.analyzers.node import choose_node_version
from dockerizer.analyzers.project import analyze_project
from dockerizer.analyzers.python import choose_python_version
from dockerizer.models.architecture import (
    AppRole,
    Confidence,
    Framework,
    Language,
    PackageManager,
    ProcessKind,
    QuestionKind,
)
from dockerizer.scanner.discovery import find_app_roots, scan_project
from tests.conftest import FASTAPI_PROJECT, NEXT_PROJECT

Factory = Callable[..., Path]


# --- descoberta ----------------------------------------------------------------------------
def test_scan_ignores_dependencies_and_symlinks(make_project: Factory, tmp_path: Path) -> None:
    outside = tmp_path / "fora.txt"
    outside.write_text("segredo")
    root = make_project({"app.py": "", "node_modules/x/package.json": "{}", ".venv/a.py": ""})
    (root / "link.txt").symlink_to(outside)
    files = scan_project(root)
    assert files.files == ["app.py"]


def test_app_roots_monorepo_and_nested_markers(make_project: Factory) -> None:
    root = make_project(
        {
            "backend/requirements.txt": "flask\n",
            "backend/docs/requirements.txt": "sphinx\n",
            "frontend/package.json": "{}",
            "docs/requirements.txt": "mkdocs\n",
        }
    )
    roots = find_app_roots(scan_project(root))
    assert roots == [("backend", Language.PYTHON), ("frontend", Language.NODE)]


# --- Python --------------------------------------------------------------------------------
def test_fastapi_uv_project(make_project: Factory) -> None:
    analysis = analyze_project(make_project(FASTAPI_PROJECT))
    [app] = analysis.applications
    assert app.package_manager is PackageManager.UV
    assert app.language_version == "3.12"
    assert {Framework.FASTAPI, Framework.ALEMBIC} <= set(app.frameworks)
    web = app.process("web")
    assert web is not None and web.command is not None
    assert web.command.argv == ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    assert web.command.confidence is Confidence.DETECTED
    assert web.dev_command is not None and "--reload" in web.dev_command.argv
    assert app.migration is not None and app.migration.command == ["alembic", "upgrade", "head"]
    assert {"DATABASE_URL", "SECRET_KEY"} <= set(app.env_vars)


def test_flask_prefers_gunicorn_when_available(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "flask==3.0\ngunicorn\n",
            "wsgi.py": "from flask import Flask\napp = Flask(__name__)\n",
        }
    )
    web = analyze_project(root).applications[0].process("web")
    assert web is not None and web.command is not None
    assert web.command.argv == ["gunicorn", "--bind", "0.0.0.0:8000", "wsgi:app"]
    assert web.dev_command is not None and web.dev_command.argv[:3] == [
        "flask",
        "--app",
        "wsgi:app",
    ]


def test_flask_without_gunicorn_warns(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "flask\n",
            "app.py": "from flask import Flask\napp = Flask(__name__)\n",
        }
    )
    app = analyze_project(root).applications[0]
    web = app.process("web")
    assert web is not None and web.command is not None and web.command.argv[0] == "flask"
    assert web.port == 5000
    assert any("gunicorn" in w for w in app.warnings)


def test_flask_factory_pattern(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "flask\ngunicorn\n",
            "src/loja/__init__.py": "from flask import Flask\n\ndef create_app():\n    return Flask(__name__)\n",
        }
    )
    app = analyze_project(root).applications[0]
    assert app.python_path == "src"
    web = app.process("web")
    assert web is not None and web.command is not None
    assert web.command.argv[-1] == "loja:create_app()"


def test_django_with_gunicorn_and_migrations(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements/base.txt": "Django>=5\npsycopg[binary]\n",
            "requirements.txt": "-r requirements/base.txt\ngunicorn\n",
            "manage.py": 'os.environ.setdefault("DJANGO_SETTINGS_MODULE", "loja.settings")\n',
            "loja/__init__.py": "",
            "loja/settings.py": "SECRET_KEY = env('DJANGO_SECRET_KEY')\n",
            "loja/wsgi.py": "",
        }
    )
    app = analyze_project(root).applications[0]
    assert app.dependency_files == ["requirements.txt", "requirements/base.txt"]
    web = app.process("web")
    assert web is not None and web.command is not None
    assert web.command.argv == ["gunicorn", "loja.wsgi:application", "--bind", "0.0.0.0:8000"]
    assert app.migration is not None and app.migration.tool == "django"
    assert "DJANGO_SECRET_KEY" in app.env_vars


def test_celery_worker_and_beat(make_project: Factory) -> None:
    root = make_project(
        {
            "pyproject.toml": '[tool.poetry.dependencies]\npython = "^3.11"\ncelery = {version="*", extras=["redis"]}\n',
            "poetry.lock": "",
            "tasks/celery.py": (
                "from celery import Celery\napp = Celery('tasks')\napp.conf.beat_schedule = {}\n"
            ),
        }
    )
    app = analyze_project(root).applications[0]
    assert app.package_manager is PackageManager.POETRY
    assert app.role is AppRole.WORKER
    worker, beat = app.process("worker"), app.process("beat")
    assert worker is not None and worker.command is not None
    assert worker.command.argv == ["celery", "-A", "tasks.celery", "worker", "--loglevel=INFO"]
    assert beat is not None and beat.kind is ProcessKind.SCHEDULER


def test_procfile_takes_precedence(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "fastapi\nuvicorn\n",
            "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "Procfile": "web: uvicorn main:app --host 0.0.0.0 --port $PORT --workers 4\n",
        }
    )
    web = analyze_project(root).applications[0].process("web")
    assert web is not None and web.command is not None
    assert web.command.source == "Procfile (web)"
    assert "--workers" in web.command.argv and "8000" in web.command.argv


def test_unknown_start_command_creates_question_without_default(make_project: Factory) -> None:
    root = make_project({"requirements.txt": "requests\n", "worker.py": "print('oi')\n"})
    analysis = analyze_project(root)
    question = next(q for q in analysis.questions if q.kind is QuestionKind.COMMAND)
    assert question.default is None
    assert analysis.applications[0].process("web").command is None  # type: ignore[union-attr]


def test_fastapi_without_uvicorn_requires_confirmation(make_project: Factory) -> None:
    root = make_project(
        {
            "requirements.txt": "fastapi\n",
            "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        }
    )
    analysis = analyze_project(root)
    question = next(q for q in analysis.questions if q.kind is QuestionKind.COMMAND)
    assert question.default is not None and question.default.startswith("uvicorn main:app")


@pytest.mark.parametrize(
    ("spec", "pinned", "expected"),
    [
        (None, None, "3.12"),
        (">=3.11", None, "3.12"),
        (">=3.13", None, "3.13"),
        (">=3.9,<3.12", None, "3.9"),
        ("^3.10", None, "3.12"),
        ("==3.11.*", None, "3.11"),
        (">=3.10", "3.11.4", "3.11"),
    ],
)
def test_choose_python_version(spec: str | None, pinned: str | None, expected: str) -> None:
    assert choose_python_version(spec, pinned) == expected


# --- Node.js -------------------------------------------------------------------------------
def test_nextjs_pnpm_project(make_project: Factory) -> None:
    app = analyze_project(make_project(NEXT_PROJECT)).applications[0]
    assert app.package_manager is PackageManager.PNPM
    assert app.lockfile == "pnpm-lock.yaml"
    assert Framework.NEXTJS in app.frameworks and app.role is AppRole.FRONTEND
    assert app.build_command == ["pnpm", "run", "build"]
    web = app.process("web")
    assert web is not None and web.command is not None
    assert web.command.argv == ["pnpm", "run", "start"] and web.port == 3000
    assert app.build_args == ["NEXT_PUBLIC_API_URL"]


def test_react_vite_spa(make_project: Factory) -> None:
    pkg = {
        "scripts": {"dev": "vite --port 5174", "build": "vite build"},
        "dependencies": {"react": "18", "react-dom": "18"},
        "devDependencies": {"vite": "5"},
    }
    root = make_project(
        {
            "package.json": json.dumps(pkg),
            "package-lock.json": "{}",
            "vite.config.ts": "export default { build: { outDir: 'public_html' } }",
        }
    )
    app = analyze_project(root).applications[0]
    web = app.process("web")
    assert web is not None and web.kind is ProcessKind.STATIC
    assert app.static_output_dir == "public_html"
    assert web.dev_port == 5174
    assert web.dev_command is not None
    assert web.dev_command.argv == ["npm", "run", "dev", "--", "--host", "0.0.0.0"]


def test_express_server_port_and_main(make_project: Factory) -> None:
    pkg = {"main": "server.js", "dependencies": {"express": "4"}}
    root = make_project(
        {
            "package.json": json.dumps(pkg),
            "yarn.lock": "",
            "server.js": "const app = require('express')();\napp.listen(4000);\n",
        }
    )
    app = analyze_project(root).applications[0]
    assert app.package_manager is PackageManager.YARN and not app.yarn_berry
    web = app.process("web")
    assert web is not None and web.command is not None
    assert web.command.argv == ["node", "server.js"] and web.port == 4000


def test_tooling_only_package_json_is_ignored(make_project: Factory) -> None:
    root = make_project(
        {
            "package.json": json.dumps({"devDependencies": {"prettier": "3"}}),
            "requirements.txt": "flask\ngunicorn\n",
            "app.py": "from flask import Flask\napp = Flask(__name__)\n",
        }
    )
    analysis = analyze_project(root)
    assert [a.language for a in analysis.applications] == [Language.PYTHON]
    assert any("ferramentas de desenvolvimento" in n for n in analysis.notes)


def test_node_server_without_port_asks(make_project: Factory) -> None:
    pkg = {"scripts": {"start": "node index.js"}}
    root = make_project({"package.json": json.dumps(pkg), "index.js": "console.log(1)\n"})
    analysis = analyze_project(root)
    assert any(q.id.startswith("port:") for q in analysis.questions)


@pytest.mark.parametrize(
    ("engines", "pinned", "expected"),
    [
        (None, None, "22"),
        (">=18", None, "22"),
        (">=24", None, "24"),
        (None, "v20.11.0", "20"),
        ("20.x", None, "20"),
    ],
)
def test_choose_node_version(engines: str | None, pinned: str | None, expected: str) -> None:
    assert choose_node_version(engines, pinned) == expected


def test_existing_dockerfile_is_preserved(make_project: Factory) -> None:
    files = {**FASTAPI_PROJECT, "Dockerfile": "FROM python:3.12\n"}
    app = analyze_project(make_project(files)).applications[0]
    assert app.existing_dockerfile == "Dockerfile"


def test_analysis_does_not_modify_project(make_project: Factory) -> None:
    from tests.conftest import tree_digest

    root = make_project(FASTAPI_PROJECT)
    before = tree_digest(root)
    analyze_project(root)
    assert tree_digest(root) == before
