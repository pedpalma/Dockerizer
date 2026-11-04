"""Descoberta de arquivos relevantes do projeto.

Percorre a árvore sem seguir links simbólicos, ignora dependências/caches e nunca executa nada.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from dockerizer.constants import (
    COMPOSE_FILE_NAMES,
    IGNORED_DIRS,
    MAX_SCAN_DEPTH,
    MAX_SCAN_FILES,
    TOOL_DIR,
)
from dockerizer.fs.paths import read_text
from dockerizer.models.architecture import Language

PYTHON_MARKERS = ("pyproject.toml", "requirements.txt", "manage.py", "Pipfile", "setup.py")
NODE_MARKERS = ("package.json",)
INIT_SCRIPT_NAMES = frozenset(
    {
        "Procfile",
        "start.sh",
        "run.sh",
        "entrypoint.sh",
        "docker-entrypoint.sh",
        "boot.sh",
        "start",
        "serve.sh",
    }
)
ENV_FILE_RE = re.compile(r"^\.env(\..+)?$|^.+\.env$")
# Subdiretórios que normalmente contêm arquivos auxiliares, não aplicações independentes.
NON_APP_DIRS = frozenset({"docs", "doc", "examples", "example", "tests", "test", "scripts", "e2e"})


@dataclass
class ProjectFiles:
    """Índice imutável dos arquivos do projeto (caminhos relativos POSIX)."""

    root: Path
    files: list[str]
    truncated: bool = False
    _set: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._set = frozenset(self.files)

    def has(self, rel: str) -> bool:
        return rel in self._set

    def path(self, rel: str) -> Path:
        return self.root / rel

    def read(self, rel: str) -> str:
        return read_text(self.root / rel)

    def under(self, directory: str, suffixes: Iterable[str] | None = None) -> Iterator[str]:
        """Arquivos dentro de ``directory`` (``"."`` = raiz), opcionalmente filtrados por sufixo."""
        prefix = "" if directory in ("", ".") else directory.rstrip("/") + "/"
        wanted = tuple(suffixes) if suffixes is not None else None
        for rel in self.files:
            if prefix and not rel.startswith(prefix):
                continue
            if wanted is not None and not rel.endswith(wanted):
                continue
            yield rel


def join(directory: str, name: str) -> str:
    if directory in ("", "."):
        return name
    return f"{directory.rstrip('/')}/{name}"


def scan_project(
    root: Path, max_files: int = MAX_SCAN_FILES, max_depth: int = MAX_SCAN_DEPTH
) -> ProjectFiles:
    root = root.resolve()
    collected: list[str] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        rel_dir = current.relative_to(root)
        depth = len(rel_dir.parts)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in IGNORED_DIRS
            and not d.endswith(".egg-info")
            and not (current / d).is_symlink()
            and depth < max_depth
        )
        for name in sorted(filenames):
            full = current / name
            if full.is_symlink():
                continue
            collected.append(PurePosixPath(*rel_dir.parts, name).as_posix())
            if len(collected) >= max_files:
                truncated = True
                break
        if truncated:
            break
    return ProjectFiles(root=root, files=collected, truncated=truncated)


def _parent(rel: str) -> str:
    parent = PurePosixPath(rel).parent.as_posix()
    return "." if parent in ("", ".") else parent


def _is_inside(child: str, parent: str) -> bool:
    if parent == ".":
        return True
    return child == parent or child.startswith(parent + "/")


def find_app_roots(files: ProjectFiles) -> list[tuple[str, Language]]:
    """Encontra diretórios candidatos a aplicação.

    Marcadores aninhados dentro de uma aplicação da mesma linguagem são considerados parte dela
    (ex.: ``docs/requirements.txt``). Linguagens diferentes podem coexistir (Django + React).
    """
    candidates: dict[Language, set[str]] = {Language.PYTHON: set(), Language.NODE: set()}
    for rel in files.files:
        name = PurePosixPath(rel).name
        directory = _parent(rel)
        if any(part in NON_APP_DIRS for part in PurePosixPath(directory).parts):
            continue
        if name in PYTHON_MARKERS:
            candidates[Language.PYTHON].add(directory)
        elif name in NODE_MARKERS:
            candidates[Language.NODE].add(directory)

    roots: list[tuple[str, Language]] = []
    for language, dirs in candidates.items():
        ordered = sorted(dirs, key=lambda d: (0 if d == "." else d.count("/") + 1, d))
        kept: list[str] = []
        for directory in ordered:
            if any(_is_inside(directory, k) for k in kept):
                continue
            kept.append(directory)
        roots.extend((d, language) for d in kept)
    roots.sort(key=lambda item: (item[0] != ".", item[0], item[1].value))
    return roots


def find_existing_docker_files(files: ProjectFiles) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for rel in files.files:
        name = PurePosixPath(rel).name
        if name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile"):
            found.append((rel, "dockerfile"))
        elif name in COMPOSE_FILE_NAMES or re.match(r"^(docker-)?compose\..+\.ya?ml$", name):
            found.append((rel, "compose"))
        elif name == ".dockerignore":
            found.append((rel, "dockerignore"))
    return found


def find_env_files(files: ProjectFiles) -> list[str]:
    return [rel for rel in files.files if ENV_FILE_RE.match(PurePosixPath(rel).name)]


def find_init_scripts(files: ProjectFiles) -> list[str]:
    scripts = []
    for rel in files.files:
        path = PurePosixPath(rel)
        if path.name in INIT_SCRIPT_NAMES or (
            path.parent.name == "bin" and path.suffix in ("", ".sh")
        ):
            scripts.append(rel)
    return scripts


def is_tool_path(rel: str) -> bool:
    return rel == TOOL_DIR or rel.startswith(TOOL_DIR + "/")
