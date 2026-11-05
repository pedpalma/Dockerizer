"""Versão mínima do Docker Compose: erro claro antes de o Compose rejeitar o arquivo."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dockerizer.cli.commands import app
from dockerizer.docker.manager import compose_version_error, parse_version
from tests.conftest import FASTAPI_PROJECT, FakeDocker

Factory = Callable[..., Path]
runner = CliRunner()
OLD = "Docker Compose version v2.20.2"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Docker Compose version v2.29.7", (2, 29, 7)),
        ("2.29.7-desktop.1", (2, 29, 7)),
        ("v2.24.0\n", (2, 24, 0)),
        ("Docker Compose version 2.24", (2, 24, 0)),
        ("docker-compose version 1.29.2, build 5becea4c", (1, 29, 2)),
        ("Docker Compose version dev", None),
        ("", None),
    ],
)
def test_parse_version(text: str, expected: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == expected


def test_compose_version_error() -> None:
    error = compose_version_error((2, 23, 3))
    assert error is not None and "2.23.3" in error and "2.24.0" in error
    assert compose_version_error((2, 24, 0)) is None
    assert compose_version_error((5, 0, 0)) is None
    assert compose_version_error(None) is None  # desconhecida: o "compose config" decide


def run(*args: str, input: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, list(args), input=input)
    return result.exit_code, result.output


def test_old_compose_blocks_commands_that_read_compose_file(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project(FASTAPI_PROJECT)
    assert run("setup", str(root), "--yes")[0] == 0
    fake_docker.outputs["compose version"] = OLD
    fake_docker.calls.clear()

    code, output = run("validate", str(root))
    assert code == 1 and "2.20.2" in output and "2.24.0" in output
    assert "[dim]" not in output  # markup não vaza como texto
    for command in ("up", "down", "logs", "status"):
        code, output = run(command, str(root))
        assert code == 1 and "2.24.0" in output, command
    # Nenhum comando do Compose foi executado com o arquivo: só a consulta de versão.
    assert not [c for c in fake_docker.calls if "-p" in c]


def test_setup_with_old_compose_reports_version(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    fake_docker.outputs["compose version"] = OLD
    root = make_project(FASTAPI_PROJECT)
    code, output = run("setup", str(root), "--yes", "--start")
    assert code == 1 and "2.20.2" in output
    assert "compose up" not in fake_docker.commands()


def test_rollback_and_purge_work_with_old_compose(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    """Dados e containers nunca ficam presos a uma atualização do Compose."""
    root = make_project(FASTAPI_PROJECT)
    assert run("setup", str(root), "--yes")[0] == 0
    fake_docker.outputs["compose version"] = OLD
    fake_docker.outputs["volume ls"] = "projeto-dockerizer_postgres_data\n"
    code, output = run("purge", str(root), input="n\nprojeto-dockerizer\n")
    assert code == 0, output
    assert ["docker", "volume", "rm", "projeto-dockerizer_postgres_data"] in fake_docker.calls
    code, output = run("rollback", str(root), "--yes")
    assert code == 0, output
    assert not (root / "compose.yaml").exists()
