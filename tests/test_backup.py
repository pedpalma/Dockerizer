"""backup/restore: dumps privados e íntegros, nunca apagados, e restore sempre precedido de backup."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from dockerizer.cli.commands import app
from dockerizer.docker.manager import subprocess_runner
from dockerizer.rollback.manifest import ManifestStore
from tests.conftest import DEFAULT_DUMPS, FASTAPI_PROJECT, FakeDocker, tree_digest

Factory = Callable[..., Path]
runner = CliRunner()
PROJECT = "projeto-dockerizer"

STACK = {
    **FASTAPI_PROJECT,
    "pyproject.toml": FASTAPI_PROJECT["pyproject.toml"].replace(
        '"alembic"]', '"alembic", "redis"]'
    ),
    ".env.example": FASTAPI_PROJECT[".env.example"] + "REDIS_URL=redis://localhost:6379/0\n",
}


def run(*args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, list(args), input=input)
    return result.exit_code, result.output


def setup_stack(make_project: Factory, files: dict[str, str] | None = None) -> Path:
    root = make_project(files or STACK)
    code, output = run("setup", str(root), "--yes")
    assert code == 0, output
    services = ManifestStore(root).load().docker.services  # type: ignore[union-attr]
    assert {"postgres", "redis"} <= set(services)
    return root


def compose_calls(fake: FakeDocker, verb: str) -> list[list[str]]:
    return [c for c in fake.calls if c[:2] == ["docker", "compose"] and verb in c]


def running(*services: str) -> str:
    return "\n".join(json.dumps({"Service": s, "State": "running"}) for s in services)


def test_backup_writes_private_verified_dumps(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = setup_stack(make_project)
    code, output = run("backup", str(root))
    assert code == 0, output
    manifest = ManifestStore(root).load()
    assert manifest is not None and len(manifest.data_backups) == 1
    backup = manifest.data_backups[0]
    assert backup.reason == "manual" and {d.service for d in backup.dumps} == {"postgres", "redis"}
    directory = root / ".dockerizer" / "data-backups" / backup.id
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for dump in backup.dumps:
        path = root / ".dockerizer" / dump.file
        assert path.read_bytes() == DEFAULT_DUMPS[dump.service]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(directory.glob("*.partial"))
    # Parados: sobem só a infraestrutura (nunca a aplicação) e, sem containers antes, down.
    (up,) = compose_calls(fake_docker, "up")
    assert "--wait" in up and up[-2:] == ["postgres", "redis"]
    assert compose_calls(fake_docker, "down")
    assert "nunca apaga backups" in output


def test_backup_of_running_services_does_not_touch_them(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = setup_stack(make_project)
    fake_docker.outputs["compose ps"] = running("app", "postgres", "redis")
    fake_docker.outputs["ps --all"] = "c1\nc2\nc3\n"
    assert run("backup", str(root))[0] == 0
    assert not compose_calls(fake_docker, "up") and not compose_calls(fake_docker, "down")
    assert not compose_calls(fake_docker, "stop")


def test_failed_dump_leaves_nothing_and_restores_state(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = setup_stack(make_project)
    fake_docker.dumps["redis"] = b"<html>erro</html>"  # não é um RDB
    code, output = run("backup", str(root))
    assert code == 1 and "Falha no backup de Redis" in output
    assert not any((root / ".dockerizer" / "data-backups").iterdir())
    assert ManifestStore(root).load().data_backups == []  # type: ignore[union-attr]
    assert compose_calls(fake_docker, "down")  # serviços iniciados para o backup foram parados


def test_dry_run_backup_writes_nothing(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = setup_stack(make_project)
    before = tree_digest(root)
    code, output = run("backup", str(root), "--dry-run")
    assert code == 0 and "pg_dump" in output and "nenhum backup gerado" in output
    assert tree_digest(root) == before


def test_rollback_and_purge_never_delete_backups(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = setup_stack(make_project)
    assert run("backup", str(root))[0] == 0
    dumps = {p: p.read_bytes() for p in (root / ".dockerizer" / "data-backups").rglob("*.*")}
    fake_docker.outputs["volume ls"] = f"{PROJECT}_postgres_data\n{PROJECT}_redis_data\n"
    code, output = run("rollback", str(root), "--yes")
    assert code == 0 and "nunca os apaga" in output
    # Sem compose.yaml (revertido) não há como subir os serviços: avisa e não pergunta.
    code, output = run("purge", str(root), input=f"{PROJECT}\n")
    assert code == 0 and "compose.yaml não existe mais" in output, output
    assert dumps and all(p.read_bytes() == data for p, data in dumps.items())
    assert ManifestStore(root).load().data_backups  # type: ignore[union-attr]


def test_tool_dir_with_dumps_survives_even_without_manifest_record(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = setup_stack(make_project)
    stray = root / ".dockerizer" / "data-backups" / "manual" / "postgres.dump"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"PGDMP")
    assert run("rollback", str(root), "--yes")[0] == 0
    assert stray.read_bytes() == b"PGDMP"


def test_purge_offers_backup_first(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = setup_stack(make_project)
    fake_docker.outputs["volume ls"] = f"{PROJECT}_postgres_data\n"
    code, output = run("purge", str(root), input=f"s\n{PROJECT}\n")
    assert code == 0, output
    (backup,) = ManifestStore(root).load().data_backups  # type: ignore[union-attr]
    assert backup.reason == "antes-do-purge"
    calls = fake_docker.commands()
    assert calls.index("compose exec") < calls.index("volume rm")


def test_purge_aborts_if_backup_fails(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = setup_stack(make_project)
    fake_docker.outputs["volume ls"] = f"{PROJECT}_postgres_data\n"
    fake_docker.failures.add("compose exec")
    code, output = run("purge", str(root), input=f"s\n{PROJECT}\n")
    assert code == 1 and "Falha no backup" in output
    assert "volume rm" not in fake_docker.commands()


def test_restore_takes_safety_backup_then_restores(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = setup_stack(make_project)
    assert run("backup", str(root))[0] == 0
    original = ManifestStore(root).load().data_backups[0]  # type: ignore[union-attr]

    code, output = run("restore", str(root), input="errado\n")
    assert code == 1 and "nada foi alterado" in output and fake_docker.received == []

    fake_docker.dumps["postgres"] = b"PGDMP-estado-atual"
    fake_docker.calls.clear()
    code, output = run("restore", str(root), input=f"{PROJECT}\n")
    assert code == 0, output
    manifest = ManifestStore(root).load()
    assert manifest is not None and [b.reason for b in manifest.data_backups] == [
        "manual",
        "antes-do-restore",
    ]
    safety = manifest.data_backups[1]
    assert safety.id in output
    safety_pg = root / ".dockerizer" / next(d.file for d in safety.dumps if d.service == "postgres")
    assert safety_pg.read_bytes() == b"PGDMP-estado-atual"
    # PostgreSQL pela entrada do pg_restore; Redis copiado com o serviço parado.
    assert fake_docker.received == [("postgres", DEFAULT_DUMPS["postgres"])]
    restore_call = next(c for c in fake_docker.calls if "-T" in c and "pg_restore" in c[-1])
    assert "--single-transaction" in restore_call[-1]
    commands = fake_docker.commands()
    assert commands.index("compose stop") < commands.index("compose cp")
    cp = next(c for c in fake_docker.calls if "cp" in c)
    assert cp[-2].endswith("redis.rdb") and cp[-1] == "redis:/data/dump.rdb"
    assert original.id in output


def test_restore_refuses_running_apps_and_corrupted_backups(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = setup_stack(make_project)
    assert run("backup", str(root))[0] == 0
    fake_docker.outputs["compose ps"] = running("app", "postgres")
    code, output = run("restore", str(root), input=f"{PROJECT}\n")
    assert code == 1 and "dockerizer down" in output and fake_docker.received == []

    fake_docker.outputs["compose ps"] = ""
    backup = ManifestStore(root).load().data_backups[0]  # type: ignore[union-attr]
    (root / ".dockerizer" / backup.dumps[0].file).write_bytes(b"PGDMP adulterado")
    code, output = run("restore", str(root), input=f"{PROJECT}\n")
    assert code == 1 and "corrompido" in output and fake_docker.received == []


def test_restore_list_and_unknown_id(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = setup_stack(make_project)
    code, output = run("restore", str(root))
    assert code == 1 and "dockerizer backup" in output
    assert run("backup", str(root))[0] == 0
    code, output = run("restore", str(root), "--list")
    assert code == 0 and "postgres, redis" in output and "manual" in output
    code, output = run("restore", str(root), "--backup", "inexistente")
    assert code == 1 and "não encontrado" in output


def test_subprocess_runner_streams_binary_files(tmp_path: Path) -> None:
    source = tmp_path / "entrada.bin"
    source.write_bytes(b"\x00\xffdados")
    target = tmp_path / "saida.bin"
    target.touch(mode=0o600)
    target.write_bytes(b"conteudo-antigo-mais-longo")
    copy = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"
    result = subprocess_runner(
        [sys.executable, "-c", copy],
        capture=True,
        timeout=10,
        cwd=tmp_path,
        stdin=source,
        stdout=target,
    )
    assert result.ok and target.read_bytes() == b"sodad\xff\x00"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
