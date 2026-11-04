"""Operações de arquivo seguras: validação de caminhos, hashes e escrita atômica."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from dockerizer.constants import MAX_READ_BYTES


class PathSecurityError(Exception):
    """Caminho fora do diretório autorizado ou inseguro para escrita."""


def resolve_inside(root: Path, relative: str | Path) -> Path:
    """Resolve ``relative`` dentro de ``root`` e garante que o resultado não escape de ``root``.

    Links simbólicos são seguidos na resolução; se apontarem para fora, o caminho é rejeitado.
    """
    root_resolved = root.resolve()
    rel = Path(relative)
    if rel.is_absolute():
        raise PathSecurityError(f"Caminho absoluto não permitido: {relative}")
    if any(part == ".." for part in rel.parts):
        raise PathSecurityError(f"Caminho com '..' não permitido: {relative}")
    candidate = root_resolved / rel
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise PathSecurityError(f"Caminho fora do diretório do projeto: {relative}")
    return candidate


def ensure_writable_target(root: Path, relative: str | Path) -> Path:
    """Valida um destino de escrita: dentro do projeto, sem links simbólicos no caminho."""
    target = resolve_inside(root, relative)
    root_resolved = root.resolve()
    current = target
    while current != root_resolved and current != current.parent:
        if current.is_symlink():
            raise PathSecurityError(f"Escrita através de link simbólico recusada: {relative}")
        current = current.parent
    return target


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text(path: Path, max_bytes: int = MAX_READ_BYTES) -> str:
    """Lê texto tolerando codificações inválidas e limitando o tamanho lido."""
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    """Escreve em arquivo temporário no mesmo diretório e substitui atomicamente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            tmp.chmod(mode)
        elif path.exists():
            tmp.chmod(path.stat().st_mode & 0o777)
        else:
            tmp.chmod(0o644)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
