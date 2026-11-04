"""Infraestrutura dos testes de integração com o Docker real.

Cada teste usa um projeto com nome único (``dzit-<hex>``). Ao final, **mesmo se o teste
falhar**, todos os recursos daquele projeto Compose são removidos — e somente eles: o filtro
usa o label de projeto exclusivo do teste. Nada global (prune) é executado.

Rodar: ``uv run pytest -m docker`` (exige Docker com Compose ≥ 2.24 — ``MIN_COMPOSE_VERSION`` — e acesso à rede para
baixar imagens e dependências).
"""

from __future__ import annotations

import json
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from dockerizer.cli import commands
from dockerizer.constants import COMPOSE_PROJECT_LABEL, compose_project_name
from tests.conftest import tree_digest, write_tree

BUILD_TIMEOUT = 600


def _docker(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=BUILD_TIMEOUT
    )


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("info", "--format", "{{.ServerVersion}}").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if docker_available():
        return
    skip = pytest.mark.skip(reason="Docker indisponível")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Project:
    """Projeto de teste em disco e o projeto Compose correspondente."""

    root: Path
    original: dict[str, str] = field(default_factory=dict)
    extra_containers: list[str] = field(default_factory=list)
    runner: CliRunner = field(default_factory=CliRunner)

    @property
    def compose_project(self) -> str:
        return compose_project_name(self.root.name)

    def write(self, files: dict[str, str]) -> None:
        write_tree(self.root, files)
        self.original = tree_digest(self.root)

    def cli(self, *args: str, input: str | None = None, expect: int = 0) -> str:
        result = self.runner.invoke(commands.app, [*args, str(self.root)], input=input)
        assert result.exit_code == expect, result.output
        return result.output

    # -- estado do Docker -----------------------------------------------------------------
    def _labeled(self, kind: str) -> list[str]:
        label = f"label={COMPOSE_PROJECT_LABEL}={self.compose_project}"
        args = ["ps", "-a"] if kind == "container" else [kind, "ls"]
        result = _docker(*args, "--quiet", "--filter", label)
        return result.stdout.split()

    def containers(self) -> list[str]:
        return self._labeled("container")

    def volumes(self) -> list[str]:
        return self._labeled("volume")

    def networks(self) -> list[str]:
        return self._labeled("network")

    def images(self) -> list[str]:
        result = _docker("image", "ls", "--format", "{{.Repository}}")
        return [i for i in result.stdout.split() if i.startswith(self.compose_project + "-")]

    def services(self) -> dict[str, dict[str, Any]]:
        result = _docker("compose", "-p", self.compose_project, "ps", "--all", "--format", "json")
        text = result.stdout.strip()
        items = (
            json.loads(text)
            if text.startswith("[")
            else [json.loads(line) for line in text.splitlines() if line.strip()]
        )
        return {item["Service"]: item for item in items}

    def wait_healthy(self, *names: str, timeout: float = 240) -> dict[str, dict[str, Any]]:
        deadline = time.monotonic() + timeout
        services: dict[str, dict[str, Any]] = {}
        while time.monotonic() < deadline:
            services = self.services()
            # Health vazio = serviço sem healthcheck (basta estar rodando).
            if all(
                s in services
                and services[s]["State"] == "running"
                and services[s].get("Health", "") in ("healthy", "")
                for s in names
            ):
                return services
            time.sleep(2)
        logs = _docker("compose", "-p", self.compose_project, "logs", "--tail", "40").stdout
        raise AssertionError(f"Serviços não ficaram saudáveis: {services}\n{logs}")

    def host_port(self, service: str) -> int:
        compose = yaml.safe_load((self.root / "compose.yaml").read_text())
        entry = str(compose["services"][service]["ports"][0])
        return int(entry.split(":")[-2])

    def published_ip(self, service: str) -> str | None:
        compose = yaml.safe_load((self.root / "compose.yaml").read_text())
        parts = str(compose["services"][service]["ports"][0]).split(":")
        return parts[0] if len(parts) == 3 else None

    def get(self, service: str, path: str, timeout: float = 60) -> tuple[int, str]:
        url = f"http://127.0.0.1:{self.host_port(service)}{path}"
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    return response.status, response.read().decode()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()
            except OSError as exc:
                last = exc
                time.sleep(1)
        raise AssertionError(f"{url} não respondeu: {last}")

    def assert_clean(self) -> None:
        """Nenhum recurso Docker restante e o projeto idêntico ao original."""
        assert self.containers() == [], "containers restantes"
        assert self.volumes() == [], "volumes restantes"
        assert self.networks() == [], "redes restantes"
        assert self.images() == [], "imagens restantes"
        assert tree_digest(self.root) == self.original, "projeto diferente do original"

    def cleanup(self) -> None:
        for container in self.containers() + self.extra_containers:
            _docker("rm", "--force", container)
        for network in self.networks():
            _docker("network", "rm", network)
        for volume in self.volumes():
            _docker("volume", "rm", volume)  # volumes exclusivos deste teste
        for image in self.images():
            _docker("image", "rm", "--force", image)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Project]:
    # Docker e verificação de portas reais (os testes unitários os simulam).
    monkeypatch.setattr(commands, "DOCKER_RUNNER", None)
    root = tmp_path / f"dzit-{secrets.token_hex(3)}"
    root.mkdir()
    created = Project(root)
    try:
        yield created
    finally:
        created.cleanup()


@pytest.fixture
def host_postgres(project: Project) -> Callable[[str, str, str], int]:
    """PostgreSQL "instalado na máquina": um container publicado numa porta do host."""

    def start(user: str, password: str, database: str) -> int:
        port = free_port()
        name = f"{project.root.name}-hostdb"
        _docker(
            "run", "-d", "--name", name, "-e", f"POSTGRES_USER={user}",
            "-e", f"POSTGRES_PASSWORD={password}", "-e", f"POSTGRES_DB={database}",
            "-p", f"{port}:5432", "postgres:17-alpine", check=True,
        )  # fmt: skip
        project.extra_containers.append(name)
        # A imagem reinicia o servidor após a inicialização: repete até o SQL passar.
        sql = "create table pedidos(id int); insert into pedidos values (1), (2), (3);"
        deadline = time.monotonic() + 90
        while True:
            result = _docker("exec", name, "psql", "-U", user, "-d", database, "-qc", sql)
            if result.returncode == 0:
                return port
            if time.monotonic() > deadline:
                raise AssertionError(f"PostgreSQL do host não iniciou: {result.stderr}")
            time.sleep(1)

    return start
