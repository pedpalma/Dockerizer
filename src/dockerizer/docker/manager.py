"""Interação com Docker CLI e Docker Compose.

- Comandos são executados como lista de argumentos (sem shell), com timeout.
- Toda operação é restrita ao projeto Compose registrado (``-p <projeto>`` ou filtro por label).
- Operações globais (``prune``) e remoção de volumes fora do fluxo explícito de ``purge`` são
  bloqueadas por uma guarda, mesmo que algum chamador tente executá-las.
- Em dry-run, comandos que alteram estado são apenas exibidos.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dockerizer.constants import (
    COMPOSE_FILE,
    COMPOSE_PROJECT_LABEL,
    MANAGED_LABEL,
    MIN_COMPOSE_VERSION,
)

DEFAULT_TIMEOUT = 30.0
LONG_TIMEOUT: float | None = None  # build/up podem demorar; interrompíveis com Ctrl+C
# "Docker Compose version v2.29.7", "2.29.7-desktop.1", "v2.24.0"...
VERSION_RE = re.compile(r"\bv?(\d+)\.(\d+)(?:\.(\d+))?")

Version = tuple[int, int, int]


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner(Protocol):
    def __call__(
        self,
        args: Sequence[str],
        *,
        capture: bool,
        timeout: float | None,
        cwd: Path,
        stdin: Path | None = None,
        stdout: Path | None = None,
    ) -> CommandResult: ...


def subprocess_runner(
    args: Sequence[str],
    *,
    capture: bool,
    timeout: float | None,
    cwd: Path,
    stdin: Path | None = None,
    stdout: Path | None = None,
) -> CommandResult:
    """Executa sem shell. ``stdin``/``stdout`` ligam a entrada/saída a arquivos (dumps binários)."""
    argv = list(args)
    try:
        if stdin is None and stdout is None:
            proc = subprocess.run(
                argv, cwd=cwd, capture_output=capture, text=True, timeout=timeout, check=False
            )
            return CommandResult(argv, proc.returncode, proc.stdout or "", proc.stderr or "")
        with ExitStack() as stack:
            source: Any = stack.enter_context(stdin.open("rb")) if stdin else subprocess.DEVNULL
            target: Any = subprocess.PIPE
            if stdout:
                # O arquivo de destino já existe (criado com permissão 600): só é truncado.
                target = stack.enter_context(stdout.open("r+b"))
                target.truncate()
            raw = subprocess.run(
                argv, cwd=cwd, stdin=source, stdout=target, stderr=subprocess.PIPE,
                timeout=timeout, check=False,
            )  # fmt: skip
        out = raw.stdout.decode(errors="replace") if isinstance(raw.stdout, bytes) else ""
        return CommandResult(argv, raw.returncode, out, raw.stderr.decode(errors="replace"))
    except FileNotFoundError:
        return CommandResult(argv, 127, "", f"Executável não encontrado: {argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(argv, 124, "", f"Tempo esgotado ao executar: {' '.join(argv)}")


class UnsafeDockerOperation(Exception):
    """Tentativa de executar operação Docker global ou destrutiva fora do fluxo autorizado."""


def guard(args: Sequence[str], allow_volume_removal: bool = False) -> None:
    tokens = set(args)
    if "prune" in tokens:
        raise UnsafeDockerOperation("Operações 'prune' nunca são executadas pelo Dockerizer.")
    if "compose" in tokens and "down" in tokens and tokens & {"-v", "--volumes", "--rmi"}:
        raise UnsafeDockerOperation("'docker compose down' com remoção de volumes é bloqueado.")
    is_volume_rm = len(args) >= 3 and args[1] == "volume" and args[2] in ("rm", "remove")
    if is_volume_rm and not allow_volume_removal:
        raise UnsafeDockerOperation("Remoção de volumes só é permitida pelo comando 'purge'.")


class DockerManager:
    def __init__(
        self,
        root: Path,
        project: str,
        compose_file: str = COMPOSE_FILE,
        runner: Runner | None = None,
        dry_run: bool = False,
        on_command: Callable[[list[str], bool], None] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.project = project
        self.compose_file = compose_file
        self.runner: Runner = runner or subprocess_runner
        self.dry_run = dry_run
        self.on_command = on_command

    # -- execução -------------------------------------------------------------------------
    def _run(
        self,
        args: list[str],
        *,
        mutating: bool,
        capture: bool = True,
        timeout: float | None = DEFAULT_TIMEOUT,
        allow_volume_removal: bool = False,
        stdin: Path | None = None,
        stdout: Path | None = None,
    ) -> CommandResult:
        guard(args, allow_volume_removal=allow_volume_removal)
        if self.on_command and (mutating or not capture):
            self.on_command(args, self.dry_run and mutating)
        if self.dry_run and mutating:
            return CommandResult(args, 0, dry_run=True)
        if stdin is None and stdout is None:
            return self.runner(args, capture=capture, timeout=timeout, cwd=self.root)
        return self.runner(
            args, capture=capture, timeout=timeout, cwd=self.root, stdin=stdin, stdout=stdout
        )

    def compose_args(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-directory",
            str(self.root),
            "-f",
            str(self.root / self.compose_file),
            "-p",
            self.project,
            *args,
        ]

    # -- ambiente -------------------------------------------------------------------------
    def cli_available(self) -> bool:
        if shutil.which("docker") is None and self.runner is subprocess_runner:
            return False
        return self._run(["docker", "compose", "version"], mutating=False).ok

    def compose_version(self) -> Version | None:
        """Versão do plugin Compose (``None`` se indisponível ou não reconhecida)."""
        result = self._run(["docker", "compose", "version"], mutating=False)
        return parse_version(result.stdout) if result.ok else None

    def daemon_running(self) -> bool:
        result = self._run(["docker", "info", "--format", "{{.ServerVersion}}"], mutating=False)
        return result.ok

    # -- compose --------------------------------------------------------------------------
    def config(self) -> CommandResult:
        return self._run(self.compose_args("config", "--quiet"), mutating=False)

    def build(self) -> CommandResult:
        return self._run(
            self.compose_args("build"), mutating=True, capture=False, timeout=LONG_TIMEOUT
        )

    def up(self) -> CommandResult:
        return self._run(
            self.compose_args("up", "--detach"), mutating=True, capture=False, timeout=LONG_TIMEOUT
        )

    def down(self) -> CommandResult:
        """Para e remove containers e redes do projeto. Volumes nunca são removidos."""
        return self._run(
            self.compose_args("down", "--remove-orphans"),
            mutating=True,
            capture=False,
            timeout=LONG_TIMEOUT,
        )

    def logs(self, service: str | None, follow: bool, tail: int) -> CommandResult:
        args = self.compose_args("logs", "--tail", str(tail))
        if follow:
            args.append("--follow")
        if service:
            args.append(service)
        return self._run(args, mutating=False, capture=False, timeout=LONG_TIMEOUT)

    def start_services(self, services: list[str]) -> CommandResult:
        """Sobe só os serviços indicados e espera ficarem saudáveis (usado por backup/restore)."""
        return self._run(
            self.compose_args("up", "--detach", "--wait", "--wait-timeout", "180", *services),
            mutating=True,
            capture=False,
            timeout=LONG_TIMEOUT,
        )

    def stop_services(self, services: list[str]) -> CommandResult:
        return self._run(
            self.compose_args("stop", *services), mutating=True, capture=False, timeout=LONG_TIMEOUT
        )

    def exec_to_file(self, service: str, script: str, destination: Path) -> CommandResult:
        """Executa ``script`` no container e grava a saída em ``destination`` (sem passar pela
        memória nem pelo terminal: dumps podem ser grandes e binários)."""
        return self._run(
            self.compose_args("exec", "-T", service, "sh", "-c", script),
            mutating=True,
            timeout=LONG_TIMEOUT,
            stdout=destination,
        )

    def exec_from_file(self, service: str, script: str, source: Path) -> CommandResult:
        return self._run(
            self.compose_args("exec", "-T", service, "sh", "-c", script),
            mutating=True,
            timeout=LONG_TIMEOUT,
            stdin=source,
        )

    def copy_into(self, service: str, source: Path, destination: str) -> CommandResult:
        return self._run(
            self.compose_args("cp", str(source), f"{service}:{destination}"),
            mutating=True,
            timeout=LONG_TIMEOUT,
        )

    def ps(self) -> list[dict[str, Any]]:
        result = self._run(self.compose_args("ps", "--all", "--format", "json"), mutating=False)
        if not result.ok:
            raise DockerCommandError(result)
        return parse_json_lines(result.stdout)

    # -- recursos por label (independem do compose.yaml existir) ---------------------------
    def _label_filter(self) -> list[str]:
        """Recursos do projeto *e* criados pelo Dockerizer (os filtros são combinados com E)."""
        return [
            "--filter",
            f"label={COMPOSE_PROJECT_LABEL}={self.project}",
            "--filter",
            f"label={MANAGED_LABEL}=true",
        ]

    def project_containers(self) -> list[str]:
        result = self._run(
            ["docker", "ps", "--all", "--quiet", *self._label_filter()], mutating=False
        )
        return result.stdout.split() if result.ok else []

    def remove_containers(self, ids: list[str]) -> CommandResult:
        return self._run(["docker", "rm", "--force", *ids], mutating=True)

    def project_networks(self) -> list[str]:
        result = self._run(
            ["docker", "network", "ls", "--quiet", *self._label_filter()], mutating=False
        )
        return result.stdout.split() if result.ok else []

    def remove_networks(self, ids: list[str]) -> CommandResult:
        return self._run(["docker", "network", "rm", *ids], mutating=True)

    def project_volumes(self) -> list[str]:
        result = self._run(
            ["docker", "volume", "ls", "--quiet", *self._label_filter()], mutating=False
        )
        return result.stdout.split() if result.ok else []

    def remove_image(self, name: str) -> CommandResult:
        """Remove uma imagem construída pelo Dockerizer (nunca imagens base ou de terceiros)."""
        return self._run(["docker", "image", "rm", name], mutating=True)

    def remove_volumes(self, names: list[str]) -> CommandResult:
        """Remove volumes nomeados. Usado exclusivamente por ``dockerizer purge``."""
        return self._run(
            ["docker", "volume", "rm", *names], mutating=True, allow_volume_removal=True
        )


class DockerCommandError(Exception):
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        detail = (result.stderr or result.stdout).strip()
        super().__init__(f"Falha ao executar {' '.join(result.args[:4])}...: {detail}")


def parse_version(text: str) -> Version | None:
    match = VERSION_RE.search(text)
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def compose_version_error(version: Version | None) -> str | None:
    """Mensagem de erro se o Compose for antigo demais para o compose.yaml gerado."""
    if version is None or version >= MIN_COMPOSE_VERSION:
        return None
    return (
        f"Docker Compose {format_version(version)} encontrado; o Dockerizer exige "
        f"{format_version(MIN_COMPOSE_VERSION)} ou mais recente (o compose.yaml gerado usa "
        "'env_file' com 'required: false'). Atualize o Docker ou o pacote "
        "docker-compose-plugin."
    )


def parse_json_lines(text: str) -> list[dict[str, Any]]:
    """``docker compose ps --format json`` gera um array (versões antigas) ou NDJSON."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return [d for d in data if isinstance(d, dict)]
    items = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                items.append(parsed)
    return items
