"""Fluxos completos da CLI com Docker simulado."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from dockerizer.cli.commands import app
from dockerizer.rollback.manifest import ManifestStore
from tests.conftest import FASTAPI_PROJECT, FakeDocker, tree_digest

Factory = Callable[..., Path]
runner = CliRunner()


def run(*args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, list(args), input=input)
    return result.exit_code, result.output


def test_help_is_in_portuguese() -> None:
    code, output = run("--help")
    assert code == 0 and "Comandos" in output and "Analisa o projeto" in output


def test_usage_errors_are_in_portuguese() -> None:
    code, output = run("setup", "--opcao-invalida")
    assert code == 2 and "Opção inexistente" in output
    code, output = run("sertup")
    assert "Comando inexistente" in output and "Você quis" in output


def test_analyze_does_not_modify(make_project: Factory) -> None:
    root = make_project(FASTAPI_PROJECT)
    before = tree_digest(root)
    code, output = run("analyze", str(root))
    assert code == 0 and "fastapi" in output and "postgres" in output
    assert tree_digest(root) == before


def test_analyze_report(make_project: Factory) -> None:
    root = make_project(FASTAPI_PROJECT)
    code, _ = run("analyze", "--path", str(root), "--report")
    assert code == 0
    reports = list((root / ".dockerizer" / "reports").iterdir())
    assert {p.suffix for p in reports} == {".md", ".json"}
    text = "".join(p.read_text() for p in reports)
    assert "troque-isto" not in text  # valor sensível nunca vai para relatório


def test_conflicting_path_arguments(make_project: Factory, tmp_path: Path) -> None:
    root = make_project(FASTAPI_PROJECT)
    code, output = run("analyze", str(root), "--path", str(tmp_path))
    assert code == 1 and "apenas uma vez" in output


def test_setup_dry_run_has_no_side_effects(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = make_project(FASTAPI_PROJECT)
    before = tree_digest(root)
    code, output = run("setup", str(root), "--dry-run", "--yes", "--build", "--start")
    assert code == 0, output
    assert tree_digest(root) == before
    assert not (root / ".dockerizer").exists()
    assert fake_docker.calls == []
    assert "executaria" in output


def test_setup_creates_files_and_is_idempotent(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project(FASTAPI_PROJECT)
    code, output = run("setup", str(root), "--yes")
    assert code == 0, output
    for name in ("Dockerfile", ".dockerignore", "compose.yaml", ".env"):
        assert (root / name).is_file()
    assert (root / ".env.example").read_text() == FASTAPI_PROJECT[".env.example"]  # preservado
    assert (root / ".env").stat().st_mode & 0o777 == 0o600
    manifest = ManifestStore(root).load()
    assert manifest is not None and set(manifest.files) >= {"Dockerfile", "compose.yaml", ".env"}
    assert manifest.docker is not None and manifest.docker.volumes == ["postgres_data"]
    assert "compose config" in fake_docker.commands()

    snapshot = tree_digest(root)
    code, output = run("setup", str(root), "--yes")
    assert code == 0 and "já estavam atualizados" in output
    changed = {k for k in tree_digest(root) if tree_digest(root)[k] != snapshot.get(k)}
    assert changed <= {".dockerizer/manifest.json"}  # segredos não são regenerados


def test_setup_preserves_existing_docker_files(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    files = {
        **FASTAPI_PROJECT,
        "Dockerfile": "FROM python:3.12\nUSER app\n",
        "compose.yaml": "services: {}\n",
    }
    root = make_project(files)
    code, output = run("setup", str(root), "--yes")
    assert code == 3 and "conflito" in output
    assert (root / "compose.yaml").read_text() == "services: {}\n"
    assert (root / "Dockerfile").read_text() == files["Dockerfile"]
    assert not (root / ".env").exists()

    code, output = run("setup", str(root), "--yes", "--force")
    assert code == 0, output
    assert (root / "Dockerfile").read_text() == files["Dockerfile"]  # reaproveitado, nunca trocado
    assert "postgres" in (root / "compose.yaml").read_text()

    code, output = run("rollback", str(root), "--yes")
    assert code == 0, output
    assert (root / "compose.yaml").read_text() == "services: {}\n"  # original restaurado
    assert not (root / ".env").exists()


def test_setup_interactive_question(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = make_project({"requirements.txt": "requests\n", "bot.py": "print('oi')\n"})
    code, output = run("setup", str(root), "--yes")
    assert code == 1 and "sem confirmação" in output
    assert not (root / "Dockerfile").exists()

    code, output = run("setup", str(root), input="python bot.py\n8080\ns\n")
    assert code == 0, output
    dockerfile = (root / "Dockerfile").read_text()
    assert 'CMD ["python", "bot.py"]' in dockerfile and "EXPOSE 8080" in dockerfile


def test_setup_cancelled_by_user_changes_nothing(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project(FASTAPI_PROJECT)
    before = tree_digest(root)
    code, _ = run("setup", str(root), input="n\n")
    assert code == 1 and tree_digest(root) == before


def test_build_failure_keeps_files_and_skips_start(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project(FASTAPI_PROJECT)
    fake_docker.failures.add("compose build")
    code, output = run("setup", str(root), "--yes", "--build", "--start")
    assert code == 1 and "Falha na construção" in output
    assert "compose up" not in fake_docker.commands()
    assert (root / "compose.yaml").exists()
    manifest = ManifestStore(root).load()
    assert manifest is not None and manifest.runs[-1].status == "falha_build"


def test_build_and_start(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = make_project(FASTAPI_PROJECT)
    code, output = run("setup", str(root), "--yes", "--build", "--start")
    assert code == 0, output
    commands = fake_docker.commands()
    assert commands.index("compose build") < commands.index("compose up")
    manifest = ManifestStore(root).load()
    assert manifest is not None and manifest.docker is not None and manifest.docker.started


def test_dev_mode_mounts_source(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = make_project(FASTAPI_PROJECT)
    code, _ = run("setup", str(root), "--yes", "--dev")
    assert code == 0
    assert ".:/app" in (root / "compose.yaml").read_text()
    assert "--reload" in (root / "Dockerfile").read_text()


def test_lifecycle_commands(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = make_project(FASTAPI_PROJECT)
    assert run("up", str(root))[0] == 1  # sem setup
    assert run("setup", str(root), "--yes")[0] == 0
    fake_docker.outputs["compose ps"] = json.dumps(
        {
            "Service": "app",
            "State": "running",
            "Health": "healthy",
            "Publishers": [{"PublishedPort": 8000, "TargetPort": 8000}],
        }
    )
    assert run("up", str(root))[0] == 0
    code, output = run("status", str(root))
    assert code == 0 and "healthy" in output and "8000→8000" in output
    assert run("logs", str(root), "--service", "app")[0] == 0
    code, output = run("down", str(root))
    assert code == 0 and "preservados" in output
    downs = [c for c in fake_docker.calls if "down" in c]
    assert downs and all("-v" not in c and "--volumes" not in c for c in downs)
    assert run("validate", str(root))[0] == 0


def test_rollback_full_cycle_restores_project(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project({**FASTAPI_PROJECT, ".gitignore": "__pycache__/\n"})
    before = tree_digest(root)
    assert run("setup", str(root), "--yes")[0] == 0
    assert ".env" in (root / ".gitignore").read_text()
    code, _ = run("rollback", str(root), "--dry-run")
    assert code == 0 and (root / "compose.yaml").exists()
    code, output = run("rollback", str(root), "--yes")
    assert code == 0, output
    assert tree_digest(root) == before


def test_purge_requires_typed_confirmation(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = make_project(FASTAPI_PROJECT)
    assert run("setup", str(root), "--yes")[0] == 0
    fake_docker.outputs["volume ls"] = "projeto-dockerizer_postgres_data\n"
    code, output = run("purge", str(root), input="errado\n")
    assert code == 1 and "incorreta" in output
    assert "volume rm" not in fake_docker.commands()
    code, output = run("purge", str(root), input="projeto-dockerizer\n")
    assert code == 0, output
    assert ["docker", "volume", "rm", "projeto-dockerizer_postgres_data"] in fake_docker.calls


def test_setup_does_not_shadow_existing_docker_compose_yml(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    original = "services:\n  db:\n    image: postgres:16-alpine\n"
    root = make_project({**FASTAPI_PROJECT, "docker-compose.yml": original})
    before = tree_digest(root)
    code, output = run("setup", str(root), "--yes")
    assert code == 3 and "docker-compose.yml" in output and "prioriza" in output
    assert tree_digest(root) == before and not (root / "compose.yaml").exists()
    code, output = run("setup", str(root), "--yes", "--dry-run")
    assert code == 3 and "validação foi omitida" in output and "não encontrado" not in output

    code, output = run("setup", str(root), "--yes", "--force")
    assert code == 0, output
    assert (root / "compose.yaml").is_file()
    assert (root / "docker-compose.yml").read_text() == original
    assert run("rollback", str(root), "--yes")[0] == 0
    assert tree_digest(root) == before


def test_compose_project_never_reuses_directory_name(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project(FASTAPI_PROJECT)
    assert run("setup", str(root), "--yes", "--build", "--start")[0] == 0
    projects = {c[c.index("-p") + 1] for c in fake_docker.calls if "-p" in c}
    assert projects == {"projeto-dockerizer"}
    manifest = ManifestStore(root).load()
    assert manifest is not None and manifest.docker is not None
    assert manifest.docker.compose_project == "projeto-dockerizer"
