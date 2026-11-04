"""Manifesto das alterações realizadas pelo Dockerizer (``.dockerizer/manifest.json``)."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from dockerizer.analyzers.env import parse_env_file
from dockerizer.constants import BACKUPS_DIR, MANIFEST_FILE, REPORTS_DIR, TOOL_DIR
from dockerizer.fs.paths import atomic_write, read_text

MANIFEST_VERSION = 1
CREDENTIALS_FILE = "credentials.env"


def now() -> datetime:
    return datetime.now(UTC)


def new_run_id() -> str:
    return now().strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


class FileRecord(BaseModel):
    path: str
    action: Literal["created", "modified"]
    sha256: str
    original_sha256: str | None = None
    original_mode: int | None = None
    backup: str | None = None
    run_id: str
    updated_at: datetime = Field(default_factory=now)


class RunRecord(BaseModel):
    id: str
    command: str
    started_at: datetime = Field(default_factory=now)
    mode: str | None = None
    status: str = "em_andamento"
    details: list[str] = Field(default_factory=list)


class DockerResources(BaseModel):
    compose_project: str
    compose_file: str
    services: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    started: bool = False


class Manifest(BaseModel):
    version: int = MANIFEST_VERSION
    project_name: str
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    mode: str | None = None
    runs: list[RunRecord] = Field(default_factory=list)
    files: dict[str, FileRecord] = Field(default_factory=dict)
    preserved_backups: list[str] = Field(default_factory=list)
    docker: DockerResources | None = None
    # Respostas do usuário às perguntas da análise (id da pergunta → resposta).
    answers: dict[str, str] = Field(default_factory=dict)

    def record_for(self, path: str) -> FileRecord | None:
        return self.files.get(path)

    def run(self, run_id: str) -> RunRecord | None:
        return next((r for r in self.runs if r.id == run_id), None)


class ManifestError(Exception):
    """Manifesto ausente, corrompido ou de versão incompatível."""


class ManifestStore:
    """Leitura e escrita atômica do manifesto e dos diretórios auxiliares."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.tool_dir = self.root / TOOL_DIR
        self.path = self.tool_dir / MANIFEST_FILE
        self.backups_dir = self.tool_dir / BACKUPS_DIR
        self.reports_dir = self.tool_dir / REPORTS_DIR
        self.credentials_path = self.tool_dir / CREDENTIALS_FILE

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> Manifest | None:
        if not self.exists():
            return None
        if self.tool_dir.is_symlink():
            raise ManifestError(f"{TOOL_DIR} é um link simbólico; recusado por segurança.")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            manifest = Manifest.model_validate(data)
        except (json.JSONDecodeError, ValidationError, OSError) as exc:
            raise ManifestError(f"Manifesto inválido em {self.path}: {exc}") from exc
        if manifest.version > MANIFEST_VERSION:
            raise ManifestError(
                f"Manifesto criado por versão mais nova do Dockerizer (v{manifest.version})."
            )
        return manifest

    def ensure_dirs(self) -> None:
        if self.tool_dir.is_symlink():
            raise ManifestError(f"{TOOL_DIR} é um link simbólico; recusado por segurança.")
        for directory in (self.tool_dir, self.backups_dir, self.reports_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.tool_dir.chmod(0o700)

    def save(self, manifest: Manifest) -> None:
        self.ensure_dirs()
        manifest.updated_at = now()
        data = manifest.model_dump_json(indent=2).encode("utf-8") + b"\n"
        atomic_write(self.path, data, mode=0o600)

    def load_credentials(self) -> dict[str, str]:
        if not self.credentials_path.is_file():
            return {}
        return parse_env_file(read_text(self.credentials_path))

    def save_credentials(self, values: dict[str, str]) -> None:
        if not values:
            return
        merged = {**self.load_credentials(), **values}
        lines = [
            "# Credenciais da infraestrutura local geradas pelo Dockerizer.",
            "# Mantidas enquanto houver volumes preservados, para que os dados continuem acessíveis.",
            *(f"{k}={v}" for k, v in sorted(merged.items())),
        ]
        self.ensure_dirs()
        atomic_write(self.credentials_path, ("\n".join(lines) + "\n").encode(), mode=0o600)
