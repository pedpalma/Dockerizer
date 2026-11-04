"""Planejamento e aplicação de alterações em arquivos, com registro no manifesto.

Nenhum arquivo é sobrescrito silenciosamente:
- arquivos preexistentes do usuário geram conflito (ou são preservados) e só são substituídos
  com ``--force`` — sempre com backup;
- arquivos gerados pelo Dockerizer e editados manualmente depois também geram conflito;
- links simbólicos nunca são seguidos para escrita.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dockerizer.fs.paths import (
    PathSecurityError,
    atomic_write,
    ensure_writable_target,
    sha256_file,
    sha256_text,
)
from dockerizer.rollback.manifest import FileRecord, Manifest, ManifestStore, now


class WritePolicy(StrEnum):
    REPLACE = "substituir"  # Dockerfile/compose: conflito, substituível apenas com --force
    PRESERVE = "preservar"  # .dockerignore/.env.example: mantém o existente (salvo --force)
    APPEND = "complementar"  # .env/.gitignore: conteúdo derivado do existente


class ChangeAction(StrEnum):
    CREATE = "criar"
    UPDATE = "atualizar"
    REPLACE = "substituir"
    APPEND = "complementar"
    UNCHANGED = "inalterado"
    SKIP = "preservado"
    CONFLICT = "conflito"


WRITE_ACTIONS = frozenset(
    {ChangeAction.CREATE, ChangeAction.UPDATE, ChangeAction.REPLACE, ChangeAction.APPEND}
)


@dataclass
class GeneratedFile:
    path: str
    content: str
    policy: WritePolicy
    description: str
    mode: int | None = None
    sensitive: bool = False


@dataclass
class PlannedChange:
    file: GeneratedFile
    action: ChangeAction
    reason: str
    backup_current: bool = False

    @property
    def writes(self) -> bool:
        return self.action in WRITE_ACTIONS


def plan_changes(
    root: Path, files: list[GeneratedFile], manifest: Manifest | None, force: bool
) -> list[PlannedChange]:
    changes: list[PlannedChange] = []
    for generated in files:
        changes.append(_plan_one(root, generated, manifest, force))
    return changes


def _plan_one(
    root: Path, generated: GeneratedFile, manifest: Manifest | None, force: bool
) -> PlannedChange:
    try:
        target = ensure_writable_target(root, generated.path)
    except PathSecurityError as exc:
        return PlannedChange(generated, ChangeAction.CONFLICT, str(exc))
    record = manifest.record_for(generated.path) if manifest else None
    if target.is_symlink():
        return PlannedChange(generated, ChangeAction.CONFLICT, "É um link simbólico; recusado.")
    if not target.exists():
        if record and record.action == "modified":
            if force:
                return PlannedChange(generated, ChangeAction.CREATE, "Recriado (--force).")
            return PlannedChange(
                generated,
                ChangeAction.CONFLICT,
                "Arquivo modificado pelo Dockerizer foi removido manualmente.",
            )
        return PlannedChange(generated, ChangeAction.CREATE, "Novo arquivo.")
    if not target.is_file():
        return PlannedChange(generated, ChangeAction.CONFLICT, "O caminho existe e não é arquivo.")

    current_sha = sha256_file(target)
    if current_sha == sha256_text(generated.content):
        return PlannedChange(generated, ChangeAction.UNCHANGED, "Conteúdo já atualizado.")
    if record is not None:
        if current_sha == record.sha256:
            return PlannedChange(
                generated, ChangeAction.UPDATE, "Gerado anteriormente; atualizado."
            )
        if force:
            return PlannedChange(
                generated,
                ChangeAction.UPDATE,
                "Editado manualmente; substituído com --force (cópia salva em backups).",
                backup_current=True,
            )
        return PlannedChange(
            generated,
            ChangeAction.CONFLICT,
            "Gerado pelo Dockerizer e editado manualmente depois. Use --force para substituir "
            "(a versão atual será salva em backup).",
        )
    if generated.policy is WritePolicy.APPEND:
        return PlannedChange(generated, ChangeAction.APPEND, "Complementado (backup do original).")
    if force:
        return PlannedChange(
            generated,
            ChangeAction.REPLACE,
            "Arquivo preexistente substituído (--force, com backup).",
        )
    if generated.policy is WritePolicy.PRESERVE:
        return PlannedChange(generated, ChangeAction.SKIP, "Arquivo preexistente preservado.")
    return PlannedChange(
        generated,
        ChangeAction.CONFLICT,
        "Arquivo preexistente não gerado pelo Dockerizer. Use --force para substituí-lo "
        "(o original será salvo em backup e restaurado no rollback).",
    )


def _backup(store: ManifestStore, run_id: str, rel: str, source: Path) -> str:
    destination = store.backups_dir / run_id / rel
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(0o600)
    return destination.relative_to(store.tool_dir).as_posix()


def apply_changes(
    root: Path,
    changes: list[PlannedChange],
    store: ManifestStore,
    manifest: Manifest,
    run_id: str,
) -> list[str]:
    """Aplica as mudanças planejadas. O manifesto é salvo **antes** de cada escrita, de modo
    que uma interrupção nunca deixa arquivos gerados sem registro."""
    written: list[str] = []
    for change in changes:
        if not change.writes:
            continue
        rel = change.file.path
        target = ensure_writable_target(root, rel)
        record = manifest.files.get(rel)
        new_sha = sha256_text(change.file.content)
        if change.action in (ChangeAction.REPLACE, ChangeAction.APPEND):
            original_sha = sha256_file(target)
            original_mode = target.stat().st_mode & 0o777
            backup = _backup(store, run_id, rel, target)
            record = FileRecord(
                path=rel,
                action="modified",
                sha256=new_sha,
                original_sha256=original_sha,
                original_mode=original_mode,
                backup=backup,
                run_id=run_id,
            )
        elif record is not None:
            if change.backup_current and target.exists():
                manifest.preserved_backups.append(_backup(store, run_id, rel, target))
            record = record.model_copy(
                update={"sha256": new_sha, "run_id": run_id, "updated_at": now()}
            )
        else:
            record = FileRecord(path=rel, action="created", sha256=new_sha, run_id=run_id)
        manifest.files[rel] = record
        store.save(manifest)
        atomic_write(target, change.file.content.encode("utf-8"), mode=change.file.mode)
        written.append(rel)
    return written
