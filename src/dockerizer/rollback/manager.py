"""Reversão das alterações registradas no manifesto e remoção explícita de dados (purge).

Garantias:
- arquivos só são removidos/restaurados se o hash atual for igual ao que o Dockerizer gravou;
  modificações manuais posteriores geram conflito (com ``--force``, a versão atual é salva em
  backup antes);
- arquivos modificados só são restaurados a partir de backup íntegro (hash conferido) — sem
  backup íntegro, nem ``--force`` prossegue;
- containers e redes do projeto são removidos; **volumes nunca**. A remoção de volumes é uma
  operação separada (``purge``) com confirmação própria.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dockerizer.constants import COMPOSE_FILE, compose_project_name
from dockerizer.docker.manager import DockerManager
from dockerizer.fs.paths import PathSecurityError, atomic_write, ensure_writable_target, sha256_file
from dockerizer.rollback.manifest import FileRecord, Manifest, ManifestStore, new_run_id


class ItemStatus(StrEnum):
    REMOVE = "remover"
    RESTORE = "restaurar"
    GONE = "já removido"
    CONFLICT = "conflito"


@dataclass
class RollbackItem:
    record: FileRecord
    status: ItemStatus
    reason: str
    backup_current: bool = False


@dataclass
class RollbackPlan:
    items: list[RollbackItem]

    @property
    def conflicts(self) -> list[RollbackItem]:
        return [i for i in self.items if i.status is ItemStatus.CONFLICT]


@dataclass
class RollbackResult:
    removed: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    conflicts: list[RollbackItem] = field(default_factory=list)
    safety_backups: list[str] = field(default_factory=list)
    volumes_preserved: list[str] = field(default_factory=list)
    docker_messages: list[str] = field(default_factory=list)
    docker_cleaned: bool = False
    tool_dir_removed: bool = False


def _backup_ok(store: ManifestStore, record: FileRecord) -> bool:
    if not record.backup:
        return False
    backup = store.tool_dir / record.backup
    return backup.is_file() and sha256_file(backup) == record.original_sha256


def plan_rollback(
    root: Path, store: ManifestStore, manifest: Manifest, force: bool
) -> RollbackPlan:
    items: list[RollbackItem] = []
    for record in manifest.files.values():
        items.append(_plan_item(root, store, record, force))
    return RollbackPlan(items)


def _plan_item(root: Path, store: ManifestStore, record: FileRecord, force: bool) -> RollbackItem:
    try:
        target = ensure_writable_target(root, record.path)
    except PathSecurityError as exc:
        return RollbackItem(record, ItemStatus.CONFLICT, str(exc))
    target_status = ItemStatus.REMOVE if record.action == "created" else ItemStatus.RESTORE
    if record.action == "modified" and not _backup_ok(store, record):
        return RollbackItem(
            record,
            ItemStatus.CONFLICT,
            "Backup do original ausente ou corrompido; restauração impossível com segurança.",
        )
    if not target.exists():
        if record.action == "created":
            return RollbackItem(record, ItemStatus.GONE, "Arquivo já não existe.")
        if force:
            return RollbackItem(record, ItemStatus.RESTORE, "Original restaurado (--force).")
        return RollbackItem(
            record,
            ItemStatus.CONFLICT,
            "Arquivo foi removido manualmente; use --force para restaurar o original.",
        )
    if target.is_symlink() or not target.is_file():
        return RollbackItem(record, ItemStatus.CONFLICT, "O caminho não é mais um arquivo comum.")
    if sha256_file(target) == record.sha256:
        reason = (
            "Gerado pelo Dockerizer, sem alterações."
            if record.action == "created"
            else ("Original restaurado a partir do backup.")
        )
        return RollbackItem(record, target_status, reason)
    if force:
        return RollbackItem(
            record,
            target_status,
            "Modificado manualmente; versão atual salva em backup antes de reverter (--force).",
            backup_current=True,
        )
    return RollbackItem(
        record,
        ItemStatus.CONFLICT,
        "Modificado manualmente após a geração; mantido. Use --force para reverter mesmo assim "
        "(a versão atual será salva em backup).",
    )


def _save_safety_backup(store: ManifestStore, run_id: str, rel: str, source: Path) -> str:
    destination = store.backups_dir / f"rollback-{run_id}" / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(0o600)
    return destination.relative_to(store.root).as_posix()


def _docker_cleanup(
    manifest: Manifest, docker: DockerManager | None, root: Path, result: RollbackResult
) -> None:
    if manifest.docker is None:
        result.docker_cleaned = True
        return
    if docker is None or not docker.cli_available() or not docker.daemon_running():
        if not manifest.docker.started:
            # Os serviços nunca foram iniciados pelo Dockerizer: não há containers registrados.
            result.docker_cleaned = True
            result.docker_messages.append(
                "Docker indisponível; nenhum container foi iniciado pelo Dockerizer."
            )
            return
        result.docker_messages.append(
            "Docker indisponível: containers do projeto não foram verificados. "
            "Execute o rollback novamente com o Docker ativo para removê-los."
        )
        return
    compose_record = manifest.files.get(COMPOSE_FILE)
    compose_path = root / COMPOSE_FILE
    if compose_record and compose_path.is_file():
        down = docker.down()
        if not down.ok:
            result.docker_messages.append("docker compose down falhou; tentando remoção por label.")
    leftovers = docker.project_containers()
    if leftovers:
        removal = docker.remove_containers(leftovers)
        if not removal.ok:
            result.docker_messages.append(f"Falha ao remover containers: {removal.stderr.strip()}")
            return
    networks = docker.project_networks()
    if networks:
        docker.remove_networks(networks)
    result.volumes_preserved = docker.project_volumes()
    result.docker_cleaned = True
    result.docker_messages.append(
        f"Containers e redes do projeto '{manifest.docker.compose_project}' removidos."
    )


def execute_rollback(
    root: Path,
    store: ManifestStore,
    manifest: Manifest,
    plan: RollbackPlan,
    docker: DockerManager | None,
    dry_run: bool = False,
) -> RollbackResult:
    result = RollbackResult(conflicts=plan.conflicts)
    _docker_cleanup(manifest, docker, root, result)
    if dry_run:
        for item in plan.items:
            if item.status is ItemStatus.REMOVE:
                result.removed.append(item.record.path)
            elif item.status is ItemStatus.RESTORE:
                result.restored.append(item.record.path)
        return result

    run_id = new_run_id()
    for item in plan.items:
        record = item.record
        if item.status is ItemStatus.CONFLICT:
            continue
        target = ensure_writable_target(root, record.path)
        if item.backup_current and target.is_file():
            saved = _save_safety_backup(store, run_id, record.path, target)
            manifest.preserved_backups.append(saved)
            result.safety_backups.append(saved)
        if item.status is ItemStatus.REMOVE:
            target.unlink(missing_ok=True)
            result.removed.append(record.path)
        elif item.status is ItemStatus.RESTORE and record.backup:
            data = (store.tool_dir / record.backup).read_bytes()
            atomic_write(target, data, mode=record.original_mode)
            result.restored.append(record.path)
        del manifest.files[record.path]
        store.save(manifest)

    if result.docker_cleaned and manifest.docker is not None:
        manifest.docker.started = False
    result.tool_dir_removed = _finalize(store, manifest, result)
    return result


def _remove_tool_dir(store: ManifestStore) -> None:
    if store.tool_dir.is_dir() and not store.tool_dir.is_symlink():
        shutil.rmtree(store.tool_dir)


def _finalize(store: ManifestStore, manifest: Manifest, result: RollbackResult) -> bool:
    """Remove ``.dockerizer/`` apenas quando nada mais depende dele."""
    if manifest.files or not result.docker_cleaned:
        store.save(manifest)
        return False
    if result.volumes_preserved or manifest.preserved_backups:
        # Mantém manifesto (para purge) e credenciais (para reutilizar os volumes preservados).
        store.save(manifest)
        return False
    _remove_tool_dir(store)
    return True


@dataclass
class PurgePlan:
    volumes: list[str]
    running_containers: list[str]


def plan_purge(manifest: Manifest, docker: DockerManager) -> PurgePlan:
    """Volumes do projeto registrado, restritos aos volumes declarados pelo Dockerizer."""
    managed = set(manifest.docker.volumes) if manifest.docker else set()
    existing = docker.project_volumes()
    project = (
        manifest.docker.compose_project
        if manifest.docker
        else compose_project_name(manifest.project_name)
    )
    volumes = [v for v in existing if v.removeprefix(f"{project}_") in managed]
    return PurgePlan(volumes=volumes, running_containers=docker.project_containers())


def execute_purge(
    store: ManifestStore, manifest: Manifest, docker: DockerManager, plan: PurgePlan
) -> bool:
    if plan.running_containers or not plan.volumes:
        return False
    result = docker.remove_volumes(plan.volumes)
    if not result.ok or docker.dry_run:
        return result.ok
    if manifest.docker is not None:
        manifest.docker.volumes = []
    store.credentials_path.unlink(missing_ok=True)
    if not manifest.files and not manifest.preserved_backups:
        _remove_tool_dir(store)
    else:
        store.save(manifest)
    return True
