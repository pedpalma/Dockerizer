"""Diff unificado das alterações planejadas, com valores sensíveis ocultos.

Usado por ``setup --diff`` para revisar o conteúdo exato de cada arquivo antes de gravá-lo
(inclusive em ``--dry-run``). Nada é lido além dos arquivos que seriam alterados.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from dockerizer.analyzers.env import is_sensitive
from dockerizer.fs.changes import ChangeAction, PlannedChange
from dockerizer.fs.paths import read_text

MASK = "********"
ENV_ASSIGNMENT_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")
# Alterações que mudam (ou mudariam) o arquivo em disco.
DIFF_ACTIONS = frozenset(
    {
        ChangeAction.CREATE,
        ChangeAction.UPDATE,
        ChangeAction.REPLACE,
        ChangeAction.APPEND,
        ChangeAction.CONFLICT,
    }
)


def mask_line(line: str) -> str:
    """``SENHA=abc`` → ``SENHA=********`` (também para URLs com credenciais)."""
    match = ENV_ASSIGNMENT_RE.match(line)
    if not match:
        return line
    prefix, name, equals, value = match.groups()
    if value and is_sensitive(name, value.strip("\"'")):
        return f"{prefix}{name}{equals}{MASK}"
    return line


def _lines(text: str, sensitive: bool) -> list[str]:
    lines = text.splitlines(keepends=True)
    if sensitive:
        lines = [
            mask_line(line.rstrip("\n")) + ("\n" if line.endswith("\n") else "") for line in lines
        ]
    return lines


def change_diff(root: Path, change: PlannedChange) -> str | None:
    """Diff do arquivo como está hoje para o conteúdo gerado (``None`` se nada muda)."""
    if change.action not in DIFF_ACTIONS:
        return None
    path = root / change.file.path
    exists = path.is_file() and not path.is_symlink()
    current = read_text(path) if exists else ""
    sensitive = change.file.sensitive
    old = _lines(current, sensitive)
    new = _lines(change.file.content, sensitive)
    if old and not old[-1].endswith("\n"):
        old[-1] += "\n"
    diff = difflib.unified_diff(
        old,
        new,
        fromfile=f"a/{change.file.path}" if exists else "/dev/null",
        tofile=f"b/{change.file.path}",
    )
    text = "".join(diff)
    return text or None
