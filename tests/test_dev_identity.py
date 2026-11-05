"""--dev: o usuário da imagem tem o UID/GID de quem roda o Dockerizer (arquivos continuam seus)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from dockerizer.analyzers.project import analyze_project
from dockerizer.generators import plan as plan_module
from dockerizer.generators.plan import GenerationPlan, build_plan, host_identity
from dockerizer.models.architecture import Mode
from tests.conftest import FASTAPI_PROJECT, NEXT_PROJECT

Factory = Callable[..., Path]
ME = {"DOCKERIZER_UID": "1234", "DOCKERIZER_GID": "5678"}


def plan_for(root: Path, mode: Mode, identity: dict[str, str] | None = None) -> GenerationPlan:
    analysis = analyze_project(root)
    for question in list(analysis.questions):
        assert question.default is not None, question.text
    ids = ME if identity is None else identity
    return build_plan(analysis, root, mode, {}, {}, secret=lambda: "segredo", identity=lambda: ids)


def file(plan: GenerationPlan, path: str) -> str:
    return next(f.content for f in plan.files if f.path == path)


def app_build(plan: GenerationPlan, service: str = "app") -> dict[str, Any]:
    document = yaml.safe_load(file(plan, "compose.yaml"))
    return dict(document["services"][service]["build"])


def test_python_dev_image_uses_host_ids(make_project: Factory) -> None:
    plan = plan_for(make_project(FASTAPI_PROJECT), Mode.DEVELOPMENT)
    dockerfile = file(plan, "Dockerfile")
    assert "ARG APP_UID=10001" in dockerfile and "ARG APP_GID=10001" in dockerfile
    assert '--non-unique --gid "$APP_GID" app' in dockerfile
    assert "--no-log-init" in dockerfile  # UID alto não gera um lastlog enorme
    assert app_build(plan)["args"] == {
        "APP_UID": "${DOCKERIZER_UID:-10001}",
        "APP_GID": "${DOCKERIZER_GID:-10001}",
    }
    env = file(plan, ".env")
    assert "DOCKERIZER_UID=1234" in env and "DOCKERIZER_GID=5678" in env
    # O serviço de migração usa a mesma imagem: mesmos argumentos de build.
    assert app_build(plan, "app-migrate")["args"] == app_build(plan)["args"]


def test_production_image_is_unchanged(make_project: Factory) -> None:
    plan = plan_for(make_project(FASTAPI_PROJECT), Mode.PRODUCTION)
    dockerfile = file(plan, "Dockerfile")
    assert "APP_UID" not in dockerfile and "groupadd --system --gid 10001 app" in dockerfile
    assert "args" not in app_build(plan)
    assert "DOCKERIZER_UID" not in file(plan, ".env")


def test_node_dev_image_recreates_node_user(make_project: Factory) -> None:
    plan = plan_for(make_project(NEXT_PROJECT), Mode.DEVELOPMENT)
    dockerfile = file(plan, "Dockerfile")
    assert "ARG APP_UID=1000" in dockerfile and 'echo "node:x:$APP_GID:"' in dockerfile
    assert dockerfile.index("ARG APP_UID") < dockerfile.index("RUN chown node:node /app")
    assert app_build(plan)["args"]["APP_UID"] == "${DOCKERIZER_UID:-1000}"


def test_existing_env_gets_ids_appended_and_keeps_values(make_project: Factory) -> None:
    root = make_project({**FASTAPI_PROJECT, ".env": "DEBUG=true\nDOCKERIZER_GID=42\n"})
    env = file(plan_for(root, Mode.DEVELOPMENT), ".env")
    assert env.startswith("DEBUG=true\nDOCKERIZER_GID=42\n")
    assert "DOCKERIZER_UID=1234" in env and "DOCKERIZER_GID=5678" not in env


def test_root_or_windows_keep_default_user(make_project: Factory) -> None:
    plan = plan_for(make_project(FASTAPI_PROJECT), Mode.DEVELOPMENT, identity={})
    assert "DOCKERIZER_UID" not in file(plan, ".env")
    assert app_build(plan)["args"]["APP_UID"] == "${DOCKERIZER_UID:-10001}"


def test_existing_dockerfile_is_not_touched(make_project: Factory) -> None:
    files = {**FASTAPI_PROJECT, "Dockerfile": "FROM python:3.12-slim\nCMD ['python']\n"}
    plan = plan_for(make_project(files), Mode.DEVELOPMENT)
    assert "args" not in app_build(plan)
    assert "DOCKERIZER_UID" not in file(plan, ".env")


@pytest.mark.parametrize(
    ("uid", "gid", "expected"),
    [
        (1000, 1000, {"DOCKERIZER_UID": "1000", "DOCKERIZER_GID": "1000"}),
        (501, 20, {"DOCKERIZER_UID": "501", "DOCKERIZER_GID": "20"}),
        (0, 0, {}),
        (1000, 0, {}),
    ],
)
def test_host_identity(
    monkeypatch: pytest.MonkeyPatch, uid: int, gid: int, expected: dict[str, str]
) -> None:
    monkeypatch.setattr(plan_module.os, "getuid", lambda: uid)
    monkeypatch.setattr(plan_module.os, "getgid", lambda: gid)
    assert host_identity() == expected


def test_host_identity_without_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(plan_module.os, "getuid")
    assert host_identity() == {}
