"""Backup e restauração lógicos dos serviços de dados locais (``dockerizer backup``/``restore``).

- Os dumps são gerados *dentro* do container de cada serviço (``pg_dump``, ``mysqldump``,
  ``SAVE`` do Redis), com as credenciais do próprio container: nenhuma senha passa pela linha
  de comando do host.
- Ficam em ``.dockerizer/data-backups/<id>/`` (permissão 600, diretórios 700), com SHA-256
  registrado no manifesto. O Dockerizer **nunca** os remove — nem no ``rollback`` nem no
  ``purge``; apagá-los é decisão do usuário.
- Serviços parados são iniciados só para a operação (apenas a infraestrutura, nunca as
  aplicações) e voltam ao estado anterior em seguida, mesmo em caso de falha.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from dockerizer.docker.manager import DockerCommandError, DockerManager
from dockerizer.fs.paths import sha256_file
from dockerizer.rollback.manifest import (
    BackupReason,
    DataBackup,
    DumpRecord,
    Manifest,
    ManifestStore,
    new_run_id,
)


@dataclass(frozen=True)
class DumpSpec:
    label: str
    filename: str
    dump_script: str
    # ``None``: restaurado copiando o arquivo para ``restore_path`` com o serviço parado.
    restore_script: str | None
    restore_path: str = ""
    magic: bytes | None = None


DUMP_SPECS: dict[str, DumpSpec] = {
    "postgres": DumpSpec(
        "PostgreSQL",
        "postgres.dump",
        'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom',
        # --clean recria os objetos do dump; tudo numa transação: falhou, nada muda.
        'exec pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists '
        "--no-owner --single-transaction --exit-on-error",
        magic=b"PGDMP",
    ),
    "mysql": DumpSpec(
        "MySQL/MariaDB",
        "mysql.sql",
        # A senha vai pelo ambiente do próprio container (MYSQL_PWD), nunca por argumento.
        'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; '
        'dump="$(command -v mysqldump || command -v mariadb-dump)"; '
        'exec "$dump" -u root --single-transaction --routines --triggers --events '
        '"$MYSQL_DATABASE"',
        'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; '
        'client="$(command -v mysql || command -v mariadb)"; '
        'exec "$client" -u root "$MYSQL_DATABASE"',
    ),
    "redis": DumpSpec(
        "Redis",
        "redis.rdb",
        '[ "$(redis-cli SAVE)" = OK ] || exit 1; '
        'dir="$(redis-cli CONFIG GET dir | tail -n 1)"; '
        'file="$(redis-cli CONFIG GET dbfilename | tail -n 1)"; '
        'exec cat "$dir/$file"',
        None,
        restore_path="/data/dump.rdb",
        magic=b"REDIS",
    ),
}
UNSUPPORTED = {
    "rabbitmq": (
        "RabbitMQ não entra no backup: mensagens em fila não são exportáveis de forma segura. "
        "Esvazie as filas antes de apagar o volume."
    ),
}


class BackupError(Exception):
    """Falha ao gerar ou restaurar um backup (nada foi apagado)."""


def data_services(manifest: Manifest) -> tuple[list[str], list[str]]:
    """Serviços do projeto com backup suportado e avisos sobre os demais."""
    services = manifest.docker.services if manifest.docker else []
    supported = [s for s in services if s in DUMP_SPECS]
    warnings = [UNSUPPORTED[s] for s in services if s in UNSUPPORTED]
    if manifest.docker:
        infra = {f"{s}_data" for s in (*DUMP_SPECS, *UNSUPPORTED)}
        app_volumes = [v for v in manifest.docker.volumes if v not in infra]
        if app_volumes:
            warnings.append(
                "Volumes de dados das aplicações não entram no backup lógico: "
                + ", ".join(app_volumes)
                + "."
            )
    return supported, warnings


def _running(docker: DockerManager) -> dict[str, str]:
    try:
        return {str(c.get("Service")): str(c.get("State")) for c in docker.ps()}
    except DockerCommandError as exc:
        raise BackupError(f"Não foi possível consultar os serviços: {exc}") from exc


def running_apps(docker: DockerManager, data: list[str]) -> list[str]:
    return sorted(
        s for s, state in _running(docker).items() if s not in data and state == "running"
    )


@contextmanager
def services_running(docker: DockerManager, services: list[str]) -> Iterator[None]:
    """Garante os serviços de dados em execução e restaura o estado anterior ao final."""
    states = _running(docker)
    stopped = [s for s in services if states.get(s) != "running"]
    had_containers = bool(docker.project_containers())
    if stopped:
        result = docker.start_services(stopped)
        if not result.ok:
            if not had_containers:
                docker.down()
            raise BackupError("Não foi possível iniciar " + ", ".join(stopped) + ".")
    try:
        yield
    finally:
        if stopped:
            # Nada existia antes: remove containers e redes (volumes nunca). Senão só para.
            if had_containers:
                docker.stop_services(stopped)
            else:
                docker.down()


def _private_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)


def _valid_dump(path: Path, spec: DumpSpec) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if spec.magic is None:
        return True
    with path.open("rb") as handle:
        return handle.read(len(spec.magic)) == spec.magic


def _dump(docker: DockerManager, service: str, directory: Path, tool_dir: Path) -> DumpRecord:
    spec = DUMP_SPECS[service]
    target = directory / spec.filename
    partial = directory / (spec.filename + ".partial")
    _private_file(partial)
    result = docker.exec_to_file(service, spec.dump_script, partial)
    if not result.ok or not _valid_dump(partial, spec):
        detail = result.stderr.strip() or "dump vazio ou inválido"
        raise BackupError(f"Falha no backup de {spec.label}: {detail}")
    partial.rename(target)
    return DumpRecord(
        service=service,
        file=target.relative_to(tool_dir).as_posix(),
        sha256=sha256_file(target),
        size=target.stat().st_size,
    )


def create_backup(
    store: ManifestStore,
    manifest: Manifest,
    docker: DockerManager,
    services: list[str],
    reason: BackupReason = "manual",
) -> DataBackup | None:
    """Gera um dump de cada serviço. Em dry-run só exibe os comandos (``None``)."""
    if docker.dry_run:
        with services_running(docker, services):
            for service in services:
                docker.exec_to_file(service, DUMP_SPECS[service].dump_script, Path(os.devnull))
        return None
    backup = DataBackup(id=new_run_id(), reason=reason)
    store.ensure_dirs()
    store.data_backups_dir.mkdir(mode=0o700, exist_ok=True)
    directory = store.data_backups_dir / backup.id
    directory.mkdir(mode=0o700)
    try:
        with services_running(docker, services):
            for service in services:
                backup.dumps.append(_dump(docker, service, directory, store.tool_dir))
    except BaseException:
        shutil.rmtree(directory)  # só o diretório criado nesta execução, incompleto
        raise
    manifest.data_backups.append(backup)
    store.save(manifest)
    return backup


def find_backup(manifest: Manifest, backup_id: str | None) -> DataBackup | None:
    if backup_id is None:
        return manifest.data_backups[-1] if manifest.data_backups else None
    return next((b for b in manifest.data_backups if b.id == backup_id), None)


def verify_backup(store: ManifestStore, backup: DataBackup) -> list[str]:
    """Problemas de integridade (arquivo ausente ou alterado desde o backup)."""
    problems = []
    for dump in backup.dumps:
        path = store.tool_dir / dump.file
        if not path.is_file() or path.is_symlink():
            problems.append(f"{dump.file}: arquivo ausente.")
        elif sha256_file(path) != dump.sha256:
            problems.append(f"{dump.file}: conteúdo alterado desde o backup (SHA-256 diferente).")
    return problems


def restore_backup(
    store: ManifestStore, docker: DockerManager, backup: DataBackup, services: list[str]
) -> list[str]:
    """Restaura os dumps dos serviços existentes; devolve os serviços restaurados."""
    dumps = [d for d in backup.dumps if d.service in services]
    with services_running(docker, [d.service for d in dumps]):
        for dump in dumps:
            spec = DUMP_SPECS[dump.service]
            source = store.tool_dir / dump.file
            if spec.restore_script is not None:
                result = docker.exec_from_file(dump.service, spec.restore_script, source)
            else:
                # O Redis grava o próprio arquivo ao parar: copia depois disso e reinicia.
                docker.stop_services([dump.service])
                result = docker.copy_into(dump.service, source, spec.restore_path)
                started = docker.start_services([dump.service])
                result = result if not result.ok else started
            if not result.ok:
                detail = result.stderr.strip()
                raise BackupError(f"Falha ao restaurar {spec.label}: {detail}")
    return [d.service for d in dumps]
