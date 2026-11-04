"""setup --diff: conteúdo exato de cada arquivo, com segredos ocultos e sem efeitos colaterais."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from dockerizer.cli.commands import app
from dockerizer.reporting.diff import MASK, mask_line
from tests.conftest import FASTAPI_PROJECT, FakeDocker, tree_digest

Factory = Callable[..., Path]
runner = CliRunner()


def test_mask_line() -> None:
    assert mask_line("POSTGRES_PASSWORD=abc123") == f"POSTGRES_PASSWORD={MASK}"
    assert mask_line("export API_TOKEN='x'") == f"export API_TOKEN={MASK}"
    assert mask_line("DATABASE_URL=postgresql://u:p@h/db") == f"DATABASE_URL={MASK}"
    assert mask_line("DEBUG=true") == "DEBUG=true"
    assert mask_line("# comentário") == "# comentário"


def test_dry_run_diff_shows_content_and_hides_secrets(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    files = {
        **FASTAPI_PROJECT,
        ".gitignore": "__pycache__/\n",
        ".env": "SECRET_KEY=valor-real-do-usuario\nDEBUG=true\n",
    }
    root = make_project(files)
    before = tree_digest(root)
    result = runner.invoke(app, ["setup", str(root), "--dry-run", "--yes", "--diff"])
    assert result.exit_code == 0, result.output
    output = result.output
    assert "+++ b/compose.yaml" in output and "--- /dev/null" in output
    assert "--- a/.gitignore" in output and "+.env" in output  # complemento do existente
    assert "POSTGRES_PASSWORD=********" in output
    assert "valor-real-do-usuario" not in output
    assert tree_digest(root) == before and fake_docker.calls == []


def test_diff_after_setup_shows_only_real_changes(
    make_project: Factory, fake_docker: FakeDocker
) -> None:
    root = make_project(FASTAPI_PROJECT)
    assert runner.invoke(app, ["setup", str(root), "--yes"]).exit_code == 0
    result = runner.invoke(app, ["setup", str(root), "--dry-run", "--yes", "--diff"])
    assert "Nenhum arquivo seria alterado" in result.output

    result = runner.invoke(app, ["setup", str(root), "--dry-run", "--yes", "--diff", "--dev"])
    assert "--- a/Dockerfile" in result.output and "reload" in result.output


def test_conflicts_are_marked_in_diff(make_project: Factory, fake_docker: FakeDocker) -> None:
    root = make_project({**FASTAPI_PROJECT, "compose.yaml": "services: {}\n"})
    result = runner.invoke(app, ["setup", str(root), "--dry-run", "--yes", "--diff"])
    assert result.exit_code == 3
    assert "conflito: não será gravado" in result.output
    assert "-services: {}" in result.output
