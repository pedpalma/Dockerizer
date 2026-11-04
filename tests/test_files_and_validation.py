"""Proteção de arquivos (sobrescrita, caminhos, links) e validação da configuração."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dockerizer.docker.manager import DockerManager
from dockerizer.fs.changes import (
    ChangeAction,
    GeneratedFile,
    WritePolicy,
    apply_changes,
    plan_changes,
)
from dockerizer.fs.paths import PathSecurityError, resolve_inside
from dockerizer.rollback.manifest import Manifest, ManifestStore
from dockerizer.validators.validation import Level, validate
from tests.conftest import FakeDocker, write_tree

Factory = Callable[..., Path]


def gen(
    path: str, content: str = "novo\n", policy: WritePolicy = WritePolicy.REPLACE
) -> GeneratedFile:
    return GeneratedFile(path=path, content=content, policy=policy, description="teste")


def apply(
    root: Path, files: list[GeneratedFile], manifest: Manifest, force: bool = False
) -> list[ChangeAction]:
    store = ManifestStore(root)
    changes = plan_changes(root, files, manifest, force)
    apply_changes(
        root, [c for c in changes if c.action is not ChangeAction.CONFLICT], store, manifest, "run1"
    )
    return [c.action for c in changes]


# --- caminhos ----------------------------------------------------------------------------------
@pytest.mark.parametrize("rel", ["../fora", "/etc/passwd", "a/../../b"])
def test_paths_outside_root_are_rejected(tmp_path: Path, rel: str) -> None:
    with pytest.raises(PathSecurityError):
        resolve_inside(tmp_path, rel)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "fora"
    outside.mkdir()
    root = tmp_path / "proj"
    root.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(PathSecurityError):
        resolve_inside(root, "link/Dockerfile")


def test_write_through_internal_symlink_is_refused(make_project: Factory) -> None:
    root = make_project({"real.txt": "x"})
    (root / "Dockerfile").symlink_to(root / "real.txt")
    [change] = plan_changes(root, [gen("Dockerfile")], None, force=True)
    assert change.action is ChangeAction.CONFLICT
    assert (root / "real.txt").read_text() == "x"


# --- preservação de arquivos -------------------------------------------------------------------
def test_preexisting_file_conflicts_and_is_untouched(make_project: Factory) -> None:
    root = make_project({"Dockerfile": "FROM meu\n"})
    manifest = Manifest(project_name="p")
    assert apply(root, [gen("Dockerfile")], manifest) == [ChangeAction.CONFLICT]
    assert (root / "Dockerfile").read_text() == "FROM meu\n"
    assert manifest.files == {}


def test_force_replaces_with_backup(make_project: Factory) -> None:
    root = make_project({"Dockerfile": "FROM meu\n"})
    manifest = Manifest(project_name="p")
    assert apply(root, [gen("Dockerfile")], manifest, force=True) == [ChangeAction.REPLACE]
    record = manifest.files["Dockerfile"]
    assert record.action == "modified" and record.backup
    assert (root / ".dockerizer" / record.backup).read_text() == "FROM meu\n"


def test_preserve_policy_skips_existing(make_project: Factory) -> None:
    root = make_project({".dockerignore": "meu\n"})
    manifest = Manifest(project_name="p")
    actions = apply(root, [gen(".dockerignore", policy=WritePolicy.PRESERVE)], manifest)
    assert actions == [ChangeAction.SKIP]
    assert (root / ".dockerignore").read_text() == "meu\n"


def test_managed_file_update_and_manual_edit_conflict(make_project: Factory) -> None:
    root = make_project({})
    manifest = Manifest(project_name="p")
    assert apply(root, [gen("Dockerfile", "v1\n")], manifest) == [ChangeAction.CREATE]
    assert apply(root, [gen("Dockerfile", "v1\n")], manifest) == [ChangeAction.UNCHANGED]
    assert apply(root, [gen("Dockerfile", "v2\n")], manifest) == [ChangeAction.UPDATE]
    (root / "Dockerfile").write_text("editado à mão\n")
    assert apply(root, [gen("Dockerfile", "v3\n")], manifest) == [ChangeAction.CONFLICT]
    assert (root / "Dockerfile").read_text() == "editado à mão\n"
    assert apply(root, [gen("Dockerfile", "v3\n")], manifest, force=True) == [ChangeAction.UPDATE]
    [backup] = manifest.preserved_backups
    assert (root / ".dockerizer" / backup).read_text() == "editado à mão\n"


def test_manifest_is_saved_before_writing(make_project: Factory) -> None:
    root = make_project({})
    manifest = Manifest(project_name="p")
    apply(root, [gen("Dockerfile")], manifest)
    stored = ManifestStore(root).load()
    assert stored is not None and "Dockerfile" in stored.files
    assert stored.files["Dockerfile"].sha256 == manifest.files["Dockerfile"].sha256


# --- validação ---------------------------------------------------------------------------------
GOOD_COMPOSE = """
services:
  db:
    image: postgres:17-alpine
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?defina}
    healthcheck: {test: [CMD, pg_isready]}
    volumes: [db_data:/var/lib/postgresql/data]
    networks: [data]
  web:
    build: {context: ., dockerfile: Dockerfile}
    ports: ["8000:8000"]
    depends_on: {db: {condition: service_healthy}}
    networks: [data]
networks: {data: {internal: true}}
volumes: {db_data: {}}
"""
GOOD_DOCKERFILE = 'FROM python:3.12-slim\nCOPY . .\nUSER app\nCMD ["python"]\n'


def good_project(make_project: Factory, **overrides: str) -> Path:
    files = {
        "compose.yaml": GOOD_COMPOSE,
        "Dockerfile": GOOD_DOCKERFILE,
        ".dockerignore": ".env\n.git\n",
        ".env": "POSTGRES_PASSWORD=x\n",
    }
    files.update(overrides)
    return make_project(files)


def test_valid_configuration(make_project: Factory) -> None:
    assert validate(good_project(make_project)).issues == []


def test_missing_required_variable(make_project: Factory) -> None:
    result = validate(good_project(make_project, **{".env": "OUTRA=1\n"}))
    assert not result.ok and "POSTGRES_PASSWORD" in result.errors[0].message


def test_secrets_in_dockerfile_are_errors(make_project: Factory) -> None:
    dockerfile = "FROM python\nCOPY .env /app/.env\nENV API_TOKEN=abc123\nUSER app\n"
    result = validate(good_project(make_project, Dockerfile=dockerfile))
    messages = " ".join(i.message for i in result.errors)
    assert ".env" in messages and "API_TOKEN" in messages


def test_root_user_and_missing_dockerignore_warn(make_project: Factory) -> None:
    root = good_project(make_project, Dockerfile="FROM python\n")
    (root / ".dockerignore").unlink()
    result = validate(root)
    assert result.ok
    assert {i.level for i in result.issues} == {Level.WARNING}
    assert len(result.warnings) == 2


def test_compose_structure_errors(make_project: Factory) -> None:
    compose = GOOD_COMPOSE.replace("healthcheck: {test: [CMD, pg_isready]}", "ports: ['5432:5432']")
    compose = compose.replace("volumes: {db_data: {}}", "")
    compose = compose.replace(
        "networks: {data:",
        "  other:\n    image: nginx\n    ports: ['8000:80']\n    depends_on: [ghost]\n"
        "networks: {data:",
    )
    result = validate(good_project(make_project, **{"compose.yaml": compose}))
    text = " | ".join(i.message for i in result.issues)
    assert "não tem healthcheck" in text
    assert "Volume 'db_data'" in text
    assert "Porta 8000 publicada por" in text
    assert "inexistente 'ghost'" in text
    assert "publica a porta" in text  # banco exposto gera aviso


def test_port_in_use_warning(make_project: Factory) -> None:
    result = validate(good_project(make_project), port_checker=lambda port: port != 8000)
    assert any("em uso" in w.message for w in result.warnings)


def test_docker_compose_config_failure_is_reported(make_project: Factory) -> None:
    root = good_project(make_project)
    fake = FakeDocker(failures={"compose config"})
    result = validate(root, docker=DockerManager(root, "p", runner=fake))
    assert result.docker_checked and not result.ok
    assert "compose config" in fake.commands()


def test_validation_without_docker_installed(make_project: Factory) -> None:
    root = good_project(make_project)
    result = validate(root, docker=DockerManager(root, "p", runner=FakeDocker(available=False)))
    assert result.ok and not result.docker_checked
    assert any("Docker" in w.message for w in result.warnings)


def test_validation_accepts_in_memory_overrides(tmp_path: Path) -> None:
    write_tree(tmp_path, {})
    overrides = {
        "compose.yaml": GOOD_COMPOSE,
        "Dockerfile": GOOD_DOCKERFILE,
        ".dockerignore": ".env\n",
        ".env": "POSTGRES_PASSWORD=x\n",
    }
    assert validate(tmp_path, overrides=overrides).ok
    assert list(tmp_path.iterdir()) == []
