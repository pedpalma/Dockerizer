"""Fixtures compartilhadas. Nenhum teste exige daemon Docker: comandos são simulados."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from dockerizer.docker.manager import CommandResult


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def tree_digest(root: Path) -> dict[str, str]:
    """Hash de todos os arquivos (para provar que nada mudou)."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


FASTAPI_PROJECT = {
    "pyproject.toml": (
        '[project]\nname = "loja"\nrequires-python = ">=3.11"\n'
        'dependencies = ["fastapi>=0.110", "uvicorn[standard]", "sqlalchemy", '
        '"psycopg[binary]", "alembic"]\n'
    ),
    "uv.lock": "version = 1\n",
    "alembic.ini": "[alembic]\nscript_location = migrations\n",
    "app/__init__.py": "",
    "app/main.py": (
        "import os\nfrom fastapi import FastAPI\n\napp = FastAPI()\n"
        'DATABASE_URL = os.getenv("DATABASE_URL")\nSECRET_KEY = os.environ["SECRET_KEY"]\n'
    ),
    ".env.example": (
        "DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/loja\n"
        "SECRET_KEY=troque-isto\nDEBUG=true\n"
    ),
}

NEXT_PROJECT = {
    "package.json": json.dumps(
        {
            "name": "site",
            "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
            "dependencies": {"next": "15.0.0", "react": "19.0.0", "react-dom": "19.0.0"},
        }
    ),
    "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
    "app/page.tsx": "export default function Page() { return process.env.NEXT_PUBLIC_API_URL }\n",
}


@pytest.fixture
def make_project(tmp_path: Path) -> Callable[..., Path]:
    def factory(files: dict[str, str], name: str = "projeto") -> Path:
        root = tmp_path / name
        root.mkdir()
        return write_tree(root, files)

    return factory


# Saídas padrão da Docker simulada (substituíveis por teste via ``outputs``).
DEFAULT_OUTPUTS = {"compose version": "Docker Compose version v2.29.7"}
DEFAULT_DUMPS = {
    "postgres": b"PGDMP\x01\x0e\x00dados-postgres",
    "mysql": b"-- MySQL dump 10.13\nINSERT INTO t VALUES (1);\n",
    "redis": b"REDIS0011\xfadados-redis",
}


@dataclass
class FakeDocker:
    """Simula a Docker CLI. ``responses`` mapeia trechos do comando para resultados."""

    calls: list[list[str]] = field(default_factory=list)
    failures: set[str] = field(default_factory=set)
    outputs: dict[str, str] = field(default_factory=dict)
    available: bool = True
    # Conteúdo gravado por "compose exec" com saída em arquivo (dumps), por serviço.
    dumps: dict[str, bytes] = field(default_factory=lambda: dict(DEFAULT_DUMPS))
    # (serviço, conteúdo) recebido por "compose exec" com entrada de arquivo (restore).
    received: list[tuple[str, bytes]] = field(default_factory=list)

    def __call__(
        self,
        args: Sequence[str],
        *,
        capture: bool,
        timeout: float | None,
        cwd: Path,
        stdin: Path | None = None,
        stdout: Path | None = None,
    ) -> CommandResult:
        from dockerizer.docker.manager import CommandResult

        argv = list(args)
        self.calls.append(argv)
        if not self.available:
            return CommandResult(argv, 127, "", "docker não encontrado")
        key = self.key(argv)
        if key in self.failures:
            return CommandResult(argv, 1, "", f"falha simulada: {key}")
        service = argv[argv.index("-T") + 1] if "-T" in argv else ""
        if stdout is not None:
            stdout.write_bytes(self.dumps.get(service, b""))
        if stdin is not None:
            self.received.append((service, stdin.read_bytes()))
        return CommandResult(argv, 0, self.outputs.get(key, DEFAULT_OUTPUTS.get(key, "")), "")

    @staticmethod
    def key(argv: list[str]) -> str:
        if argv[:2] == ["docker", "compose"] and "-p" in argv:
            rest = argv[argv.index("-p") + 2 :]
            return "compose " + (rest[0] if rest else "")
        return " ".join(argv[1:3])

    def commands(self) -> list[str]:
        return [self.key(c) for c in self.calls]


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    from dockerizer.cli import commands

    fake = FakeDocker()
    monkeypatch.setattr(commands, "DOCKER_RUNNER", fake)
    monkeypatch.setattr(commands, "PORT_CHECKER", lambda port: True)
    return fake
