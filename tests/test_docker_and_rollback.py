"""Gerenciador Docker (com mocks) e mecanismo de rollback/purge."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dockerizer.docker.manager import (
    DockerManager,
    UnsafeDockerOperation,
    guard,
    parse_json_lines,
)
from dockerizer.fs.changes import GeneratedFile, WritePolicy, apply_changes, plan_changes
from dockerizer.rollback.manager import (
    ItemStatus,
    execute_purge,
    execute_rollback,
    plan_purge,
    plan_rollback,
)
from dockerizer.rollback.manifest import DockerResources, Manifest, ManifestStore
from tests.conftest import FakeDocker

Factory = Callable[..., Path]


# --- DockerManager -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "args",
    [
        ["docker", "system", "prune", "-a"],
        ["docker", "compose", "-p", "x", "down", "-v"],
        ["docker", "compose", "-p", "x", "down", "--volumes"],
        ["docker", "volume", "rm", "x_data"],
    ],
)
def test_guard_blocks_destructive_operations(args: list[str]) -> None:
    with pytest.raises(UnsafeDockerOperation):
        guard(args)


def test_guard_allows_volume_removal_only_when_explicit() -> None:
    guard(["docker", "volume", "rm", "x_data"], allow_volume_removal=True)


def test_down_never_removes_volumes(tmp_path: Path) -> None:
    fake = FakeDocker()
    DockerManager(tmp_path, "proj", runner=fake).down()
    [call] = fake.calls
    assert "down" in call and "-v" not in call and "--volumes" not in call
    assert call[call.index("-p") + 1] == "proj"


def test_dry_run_does_not_execute_mutating_commands(tmp_path: Path) -> None:
    fake = FakeDocker()
    printed: list[tuple[list[str], bool]] = []
    manager = DockerManager(
        tmp_path, "proj", runner=fake, dry_run=True, on_command=lambda a, s: printed.append((a, s))
    )
    assert manager.build().dry_run and manager.up().dry_run and manager.down().dry_run
    assert fake.calls == []
    assert all(simulated for _, simulated in printed) and len(printed) == 3


def test_project_resources_are_filtered_by_label(tmp_path: Path) -> None:
    fake = FakeDocker(outputs={"ps --all": "abc\ndef\n"})
    ids = DockerManager(tmp_path, "proj", runner=fake).project_containers()
    assert ids == ["abc", "def"]
    assert "label=com.docker.compose.project=proj" in fake.calls[0]


def test_parse_compose_ps_formats() -> None:
    assert parse_json_lines('[{"Service": "a"}]') == [{"Service": "a"}]
    assert parse_json_lines('{"Service": "a"}\n{"Service": "b"}\n') == [
        {"Service": "a"},
        {"Service": "b"},
    ]
    assert parse_json_lines("") == []


# --- rollback ------------------------------------------------------------------------------
def setup_files(root: Path, files: list[GeneratedFile]) -> tuple[ManifestStore, Manifest]:
    store = ManifestStore(root)
    manifest = Manifest(
        project_name="proj",
        docker=DockerResources(
            compose_project="proj", compose_file="compose.yaml", volumes=["postgres_data"]
        ),
    )
    changes = plan_changes(root, files, manifest, force=False)
    apply_changes(root, changes, store, manifest, "run1")
    return store, manifest


def generated(path: str, content: str, policy: WritePolicy = WritePolicy.REPLACE) -> GeneratedFile:
    return GeneratedFile(path=path, content=content, policy=policy, description="t")


def test_rollback_removes_created_and_restores_modified(make_project: Factory) -> None:
    root = make_project({".gitignore": "node_modules\n"})
    (root / ".gitignore").chmod(0o640)
    store, manifest = setup_files(
        root,
        [
            generated("compose.yaml", "services: {}\n"),
            generated(".gitignore", "node_modules\n.env\n", WritePolicy.APPEND),
        ],
    )
    plan = plan_rollback(root, store, manifest, force=False)
    assert {i.status for i in plan.items} == {ItemStatus.REMOVE, ItemStatus.RESTORE}
    fake = FakeDocker()
    result = execute_rollback(root, store, manifest, plan, DockerManager(root, "proj", runner=fake))
    assert not (root / "compose.yaml").exists()
    assert (root / ".gitignore").read_text() == "node_modules\n"
    assert (root / ".gitignore").stat().st_mode & 0o777 == 0o640
    assert result.tool_dir_removed and not (root / ".dockerizer").exists()
    assert "compose down" in fake.commands()
    assert not any("volume rm" in c for c in fake.commands())


def test_rollback_detects_manual_changes(make_project: Factory) -> None:
    root = make_project({})
    store, manifest = setup_files(root, [generated("Dockerfile", "FROM x\n")])
    (root / "Dockerfile").write_text("FROM x\n# meu ajuste\n")
    plan = plan_rollback(root, store, manifest, force=False)
    assert [i.status for i in plan.items] == [ItemStatus.CONFLICT]
    result = execute_rollback(
        root, store, manifest, plan, DockerManager(root, "proj", runner=FakeDocker())
    )
    assert (root / "Dockerfile").read_text().endswith("# meu ajuste\n")
    assert result.conflicts and (root / ".dockerizer" / "manifest.json").exists()

    forced = plan_rollback(root, store, manifest, force=True)
    result = execute_rollback(
        root, store, manifest, forced, DockerManager(root, "proj", runner=FakeDocker())
    )
    assert not (root / "Dockerfile").exists()
    [backup] = result.safety_backups
    assert (root / backup).read_text().endswith("# meu ajuste\n")
    assert (root / ".dockerizer").exists()  # backup do usuário nunca é apagado


def test_rollback_refuses_restore_without_intact_backup(make_project: Factory) -> None:
    root = make_project({".gitignore": "a\n"})
    store, manifest = setup_files(root, [generated(".gitignore", "a\nb\n", WritePolicy.APPEND)])
    backup = store.tool_dir / (manifest.files[".gitignore"].backup or "")
    backup.write_text("corrompido")
    [item] = plan_rollback(root, store, manifest, force=True).items
    assert item.status is ItemStatus.CONFLICT


def test_rollback_preserves_volumes_and_credentials(make_project: Factory) -> None:
    root = make_project({})
    store, manifest = setup_files(root, [generated("compose.yaml", "services: {}\n")])
    store.save_credentials({"POSTGRES_PASSWORD": "abc"})
    fake = FakeDocker(outputs={"volume ls": "proj_postgres_data\n"})
    plan = plan_rollback(root, store, manifest, force=False)
    result = execute_rollback(root, store, manifest, plan, DockerManager(root, "proj", runner=fake))
    assert result.volumes_preserved == ["proj_postgres_data"]
    assert not result.tool_dir_removed
    assert store.load_credentials() == {"POSTGRES_PASSWORD": "abc"}
    assert not any(c.startswith("volume rm") for c in fake.commands())


def test_rollback_dry_run_changes_nothing(make_project: Factory) -> None:
    root = make_project({})
    store, manifest = setup_files(root, [generated("compose.yaml", "services: {}\n")])
    fake = FakeDocker()
    manager = DockerManager(root, "proj", runner=fake, dry_run=True)
    plan = plan_rollback(root, store, manifest, force=False)
    result = execute_rollback(root, store, manifest, plan, manager, dry_run=True)
    assert result.removed == ["compose.yaml"] and (root / "compose.yaml").exists()
    assert "compose down" not in fake.commands()


def test_rollback_without_docker_keeps_manifest_if_started(make_project: Factory) -> None:
    root = make_project({})
    store, manifest = setup_files(root, [generated("compose.yaml", "services: {}\n")])
    assert manifest.docker is not None
    manifest.docker.started = True
    plan = plan_rollback(root, store, manifest, force=False)
    unavailable = DockerManager(root, "proj", runner=FakeDocker(available=False))
    result = execute_rollback(root, store, manifest, plan, unavailable)
    assert not result.docker_cleaned and (root / ".dockerizer").exists()


def test_purge_requires_stopped_containers_and_removes_only_managed(make_project: Factory) -> None:
    root = make_project({})
    store, manifest = setup_files(root, [])
    fake = FakeDocker(outputs={"volume ls": "proj_postgres_data\nproj_outro\n", "ps --all": "c1\n"})
    docker = DockerManager(root, "proj", runner=fake)
    plan = plan_purge(manifest, docker)
    assert plan.volumes == ["proj_postgres_data"]
    assert not execute_purge(store, manifest, docker, plan)
    assert "volume rm" not in fake.commands()

    fake.outputs["ps --all"] = ""
    plan = plan_purge(manifest, docker)
    assert execute_purge(store, manifest, docker, plan)
    assert ["docker", "volume", "rm", "proj_postgres_data"] in fake.calls
