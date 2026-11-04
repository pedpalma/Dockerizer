"""Comandos de produção definidos em gerenciadores de processo: systemd, supervisor e pm2.

Quando o projeto já roda fora do Docker, a forma real de execução costuma estar num unit do
systemd (``ExecStart=``), num ``[program:x]`` do supervisor ou num ``ecosystem.config.js`` do
pm2. Esses arquivos são apenas **lidos** (nunca executados) e têm a mesma precedência do
``Procfile``: substituem o comando inferido a partir do framework.

Os comandos são adaptados para o container: executáveis de virtualenv viram o nome do
programa, ``127.0.0.1`` vira ``0.0.0.0``, sockets Unix viram TCP e opções que só fazem
sentido no host (``--daemon``, ``--pid``, ``--user``, arquivos de log) são removidas.
"""

from __future__ import annotations

import configparser
import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from dockerizer.models.architecture import (
    Application,
    Command,
    Framework,
    Language,
    Process,
    ProcessKind,
    ProjectAnalysis,
)
from dockerizer.scanner.discovery import ProjectFiles

PYTHON_EXECUTABLES = frozenset(
    {"python", "uvicorn", "gunicorn", "hypercorn", "daphne", "celery", "flask", "waitress-serve"}
)
NODE_EXECUTABLES = frozenset({"node", "npm", "pnpm", "yarn", "npx", "next", "tsx", "ts-node"})
WEB_SERVERS = frozenset({"uvicorn", "gunicorn", "hypercorn", "daphne", "waitress-serve"})
PM2_FILES = frozenset(
    {"ecosystem.config.js", "ecosystem.config.cjs", "ecosystem.config.mjs", "ecosystem.json"}
)
PM2_JSON_FILES = frozenset({"ecosystem.json", "pm2.json", "process.json"})
DEFAULT_PORT = 8000

# Opções por programa: com valor a remover, sem valor a remover, e logs (viram "-").
DROP_WITH_VALUE = {
    "gunicorn": {"-p", "--pid", "-u", "--user", "-g", "--group", "--chdir", "--pythonpath"},
    "celery": {"--pidfile", "--logfile", "-f", "--uid", "--gid", "--workdir"},
    "uvicorn": {"--env-file"},
}
DROP_FLAGS = {"gunicorn": {"-D", "--daemon"}, "celery": {"--detach"}}
LOG_OPTIONS = {"gunicorn": {"--access-logfile", "--error-logfile", "--log-file"}}
BIND_OPTIONS = {
    "gunicorn": {"-b", "--bind"},
    "hypercorn": {"-b", "--bind"},
    "daphne": {"-b", "--bind"},
    "uvicorn": {"--host"},
    "flask": {"-h", "--host"},
}
SOCKET_OPTIONS = {"uvicorn": {"--uds", "--fd"}, "daphne": {"-u", "--unix-socket"}}
PORT_OPTIONS = {"uvicorn": {"--port"}, "daphne": {"-p", "--port"}, "flask": {"-p", "--port"}}


@dataclass
class ProcessDefinition:
    """Um processo declarado por um gerenciador de processos."""

    manager: str  # systemd, supervisor, pm2
    file: str
    name: str
    argv: list[str]
    cwd: str | None = None  # relativo à raiz do projeto, ou absoluto (caminho do servidor)
    env: dict[str, str] = field(default_factory=dict)
    instances: int | None = None
    warnings: list[str] = field(default_factory=list)
    app: str | None = None  # aplicação já conhecida (ex.: serviço do Compose que a constrói)
    dev: bool = False  # comando de desenvolvimento (reload): vira dev_command, não o principal

    @property
    def source(self) -> str:
        return f"{self.manager} ({self.file}: {self.name})"


# -- descoberta ---------------------------------------------------------------------------
def find_process_definitions(files: ProjectFiles) -> tuple[list[ProcessDefinition], list[str]]:
    definitions: list[ProcessDefinition] = []
    notes: list[str] = []
    for rel in files.files:
        name = PurePosixPath(rel).name
        try:
            if name.endswith(".service"):
                definitions.extend(_systemd(rel, files.read(rel), notes))
            elif name in PM2_FILES or name in PM2_JSON_FILES:
                definitions.extend(_pm2(rel, files.read(rel), notes))
            elif name.endswith((".conf", ".ini")):
                text = files.read(rel)
                if "[program:" in text:
                    definitions.extend(_supervisor(rel, text, notes))
        except (ValueError, configparser.Error, json.JSONDecodeError) as exc:
            notes.append(f"{rel}: não foi possível interpretar ({exc}).")
    return definitions, notes


def _relative_cwd(config_rel: str, cwd: str | None) -> str | None:
    """Diretório de trabalho relativo à raiz do projeto (absolutos são mantidos)."""
    if not cwd:
        return None
    if cwd.startswith("/"):
        return cwd.rstrip("/") or "/"
    base = posixpath.dirname(config_rel)
    joined = posixpath.normpath(posixpath.join(base, cwd))
    return None if joined.startswith("..") else joined


def _systemd(rel: str, text: str, notes: list[str]) -> list[ProcessDefinition]:
    section = ""
    values: dict[str, list[str]] = {}
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append(buffer + line)
        buffer = ""
    for line in logical:
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Service" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())
    if values.get("Type", [""])[-1] == "oneshot":
        return []
    starts = [v for v in values.get("ExecStart", []) if v]
    if not starts:
        return []
    exec_start = starts[-1].lstrip("-@+!:")
    name = PurePosixPath(rel).name.removesuffix(".service").rstrip("@")
    if "%" in exec_start:
        notes.append(f"{rel}: ExecStart usa especificadores do systemd (%); ignorado.")
        return []
    env: dict[str, str] = {}
    for entry in values.get("Environment", []):
        for token in shlex.split(entry):
            key, sep, val = token.partition("=")
            if sep:
                env[key] = val
    definition = ProcessDefinition(
        manager="systemd",
        file=rel,
        name=name,
        argv=shlex.split(exec_start),
        cwd=_relative_cwd(rel, values.get("WorkingDirectory", [None])[-1]),
        env=env,
    )
    if values.get("EnvironmentFile"):
        definition.warnings.append(
            f"{rel} carrega EnvironmentFile={values['EnvironmentFile'][-1]}; no container as "
            "variáveis vêm do .env."
        )
    return [definition]


def _supervisor(rel: str, text: str, notes: list[str]) -> list[ProcessDefinition]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    definitions = []
    for section in parser.sections():
        if not section.startswith("program:"):
            continue
        program = parser[section]
        command = program.get("command", "").strip()
        if not command:
            continue
        name = section.removeprefix("program:")
        if "%(" in command:
            notes.append(f"{rel} [{section}]: comando usa expansões do supervisor; ignorado.")
            continue
        env: dict[str, str] = {}
        for item in re.split(r",(?=\s*[A-Za-z_][A-Za-z0-9_]*=)", program.get("environment", "")):
            key, sep, val = item.strip().partition("=")
            if sep:
                env[key.strip()] = val.strip().strip("\"'")
        numprocs = program.get("numprocs", "1").strip()
        definitions.append(
            ProcessDefinition(
                manager="supervisor",
                file=rel,
                name=name,
                argv=shlex.split(command),
                cwd=_relative_cwd(rel, program.get("directory")),
                env=env,
                instances=int(numprocs) if numprocs.isdigit() else None,
            )
        )
    return definitions


def _matching_brace(text: str, start: int) -> int:
    depth = 0
    quote: str | None = None
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if char == quote and text[index - 1] != "\\":
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


JS_STRING = r"""(['"`])((?:(?!\1).)*)\1"""


def _js_field(block: str, key: str) -> str | None:
    match = re.search(rf"\b{key}\s*:\s*{JS_STRING}", block)
    return match.group(2) if match else None


def _pm2_objects(text: str) -> list[dict[str, object]]:
    """Extrai os objetos de ``apps`` de um ecosystem.config.js sem executá-lo."""
    apps: list[dict[str, object]] = []
    for match in re.finditer(r"\bscript\s*:", text):
        start = text.rfind("{", 0, match.start())
        while start >= 0:
            end = _matching_brace(text, start)
            if end > match.start():
                break
            start = text.rfind("{", 0, start)
        if start < 0:
            continue
        end = _matching_brace(text, start)
        block = text[start : end + 1]
        if any(a.get("_start") == start for a in apps):
            continue
        item: dict[str, object] = {"_start": start}
        for key in ("name", "script", "cwd", "interpreter", "exec_mode"):
            value = _js_field(block, key)
            if value is not None:
                item[key] = value
        args = _js_field(block, "args")
        if args is None:
            array = re.search(r"\bargs\s*:\s*\[([^\]]*)\]", block)
            if array:
                args_list = [m.group(2) for m in re.finditer(JS_STRING, array.group(1))]
                item["args"] = args_list
        else:
            item["args"] = args
        instances = re.search(r"\binstances\s*:\s*['\"]?(\w+)", block)
        if instances:
            item["instances"] = instances.group(1)
        port = re.search(r"\bPORT\s*:\s*['\"]?(\d+)", block)
        if port:
            item["env"] = {"PORT": port.group(1)}
        apps.append(item)
    return apps


def _pm2(rel: str, text: str, notes: list[str]) -> list[ProcessDefinition]:
    if PurePosixPath(rel).name in PM2_JSON_FILES:
        data = json.loads(text)
        raw_apps = data.get("apps", []) if isinstance(data, dict) else data
        apps = [a for a in raw_apps if isinstance(a, dict)] if isinstance(raw_apps, list) else []
    else:
        apps = _pm2_objects(text)
    definitions = []
    for index, app in enumerate(apps):
        script = app.get("script")
        if not isinstance(script, str) or not script:
            continue
        raw_args = app.get("args", [])
        args = shlex.split(raw_args) if isinstance(raw_args, str) else [str(a) for a in raw_args]
        interpreter = app.get("interpreter")
        if isinstance(interpreter, str) and interpreter not in ("node", "none", ""):
            argv = [interpreter, script, *args]
        elif script in NODE_EXECUTABLES or ("/" in script and not script.endswith(_JS_SUFFIXES)):
            argv = [script, *args]
        elif script.endswith(_JS_SUFFIXES):
            argv = ["node", script, *args]
        else:
            argv = [script, *args]
        env_value = app.get("env")
        env = {str(k): str(v) for k, v in env_value.items()} if isinstance(env_value, dict) else {}
        instances_raw = str(app.get("instances", "1"))
        instances = int(instances_raw) if instances_raw.isdigit() else None
        definition = ProcessDefinition(
            manager="pm2",
            file=rel,
            name=str(app.get("name") or f"app{index + 1}"),
            argv=argv,
            cwd=_relative_cwd(rel, app.get("cwd") if isinstance(app.get("cwd"), str) else None),
            env=env,
            instances=instances,
        )
        if instances_raw in ("max", "-1", "0") or app.get("exec_mode") == "cluster":
            definition.warnings.append(
                f"pm2 '{definition.name}' roda em modo cluster; no Compose cada serviço tem um "
                "processo (escale com 'docker compose up --scale')."
            )
        definitions.append(definition)
    if not apps:
        notes.append(f"{rel}: nenhum app com 'script' encontrado.")
    return definitions


_JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts")


# -- normalização -------------------------------------------------------------------------
def program_of(argv: list[str]) -> str:
    """Nome do programa efetivo (``python -m uvicorn`` → ``uvicorn``)."""
    if not argv:
        return ""
    exe = PurePosixPath(argv[0]).name
    if re.fullmatch(r"python[\d.]*", exe):
        if len(argv) > 2 and argv[1] == "-m":
            return argv[2]
        if len(argv) > 1 and PurePosixPath(argv[1]).name == "manage.py":
            return "manage.py"
        return "python"
    return exe


def language_of(argv: list[str]) -> Language | None:
    program = program_of(argv)
    exe = PurePosixPath(argv[0]).name if argv else ""
    if program in PYTHON_EXECUTABLES or program == "manage.py" or exe.endswith(".py"):
        return Language.PYTHON
    if re.fullmatch(r"python[\d.]*", exe):
        return Language.PYTHON
    if program in NODE_EXECUTABLES or exe.endswith(_JS_SUFFIXES):
        return Language.NODE
    return None


def _strip_env_prefix(argv: list[str], env: dict[str, str]) -> list[str]:
    if argv and PurePosixPath(argv[0]).name == "env":
        argv = argv[1:]
        while argv and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]):
            key, _, value = argv.pop(0).partition("=")
            env[key] = value
    return argv


def _bind_value(value: str, port: int, warnings: list[str], source: str) -> tuple[str, int]:
    if value.startswith(("unix:", "/")) or value.startswith("fd://"):
        warnings.append(
            f"{source}: socket Unix ({value}) substituído por TCP na porta {port} (o nginx do "
            "host não alcança sockets dentro do container)."
        )
        return f"0.0.0.0:{port}", port
    _host, sep, maybe_port = value.rpartition(":")
    if sep and maybe_port.isdigit():
        return f"0.0.0.0:{maybe_port}", int(maybe_port)
    return "0.0.0.0", port


def normalize(definition: ProcessDefinition) -> tuple[list[str], int | None]:
    """Adapta o comando ao container. Retorna (argv, porta TCP detectada)."""
    env = definition.env
    argv = _strip_env_prefix(list(definition.argv), env)
    if not argv:
        return [], None
    port_env = env.get("PORT")
    default_port = int(port_env) if port_env and port_env.isdigit() else DEFAULT_PORT
    argv = [re.sub(r"\$\{?PORT\}?", str(default_port), a) for a in argv]
    exe = PurePosixPath(argv[0]).name
    if re.fullmatch(r"python[\d.]*", exe):
        argv[0] = "python"
    elif "/" in argv[0] and (exe in PYTHON_EXECUTABLES or exe in NODE_EXECUTABLES):
        argv[0] = exe  # /srv/app/.venv/bin/gunicorn → gunicorn
    elif argv[0].startswith("/"):
        definition.warnings.append(
            f"{definition.source}: executável {argv[0]} fica fora do projeto; confirme o comando."
        )
    program = program_of(argv)
    port: int | None = int(port_env) if port_env and port_env.isdigit() else None
    result: list[str] = []
    index = 0
    skip_value = DROP_WITH_VALUE.get(program, set())
    flags = DROP_FLAGS.get(program, set())
    logs = LOG_OPTIONS.get(program, set())
    binds = BIND_OPTIONS.get(program, set())
    sockets = SOCKET_OPTIONS.get(program, set())
    ports = PORT_OPTIONS.get(program, set())
    while index < len(argv):
        arg = argv[index]
        name, eq, inline = arg.partition("=")
        option = name if name.startswith("-") else arg
        value = inline if eq else (argv[index + 1] if index + 1 < len(argv) else "")
        consumed = 1 if eq else 2
        if option in flags:
            index += 1
            continue
        if option in skip_value:
            index += consumed
            continue
        if option in logs:
            result += [option, "-"]
            index += consumed
            continue
        if option in sockets:
            port = port or default_port
            definition.warnings.append(
                f"{definition.source}: socket Unix ({value}) substituído por TCP na porta {port}."
            )
            result += ["--host", "0.0.0.0", "--port", str(port)]
            index += consumed
            continue
        if option in binds:
            bound, bound_port = _bind_value(
                value, port or default_port, definition.warnings, definition.source
            )
            if program in ("uvicorn", "flask", "daphne"):
                bound = "0.0.0.0"
            else:
                port = bound_port
            result += [option, bound]
            index += consumed
            continue
        if option in ports and value.isdigit():
            port = int(value)
        if program == "manage.py" and arg == "runserver":
            address = argv[index + 1] if index + 1 < len(argv) else ""
            result.append(arg)
            if address and not address.startswith("-"):
                bound, port = _bind_value(
                    address, default_port, definition.warnings, definition.source
                )
                result.append(bound)
                index += 2
            else:
                port = port or DEFAULT_PORT
                result.append(f"0.0.0.0:{port}")
                index += 1
            continue
        result.append(arg)
        index += 1
    if program in ("gunicorn", "hypercorn") and port is None:
        port = DEFAULT_PORT  # padrão do gunicorn é 127.0.0.1:8000
        result += ["--bind", f"0.0.0.0:{port}"]
    if program == "uvicorn":
        if "--host" not in result:
            result += ["--host", "0.0.0.0"]
        if port is None:
            port = DEFAULT_PORT
    return result, port


# -- aplicação na análise -----------------------------------------------------------------
def _match_app(definition: ProcessDefinition, apps: list[Application]) -> Application | None:
    language = language_of(definition.argv)
    candidates = [a for a in apps if language is None or a.language is language]
    cwd = definition.cwd
    if cwd and not cwd.startswith("/"):
        inside = [
            a for a in candidates if a.path == "." or cwd == a.path or cwd.startswith(a.path + "/")
        ]
        if inside:
            return max(inside, key=lambda a: 0 if a.path == "." else len(a.path))
    if cwd and cwd.startswith("/"):
        parts = PurePosixPath(cwd).parts
        for app in sorted(candidates, key=lambda a: -len(a.path)):
            if app.path == ".":
                continue
            app_parts = PurePosixPath(app.path).parts
            if tuple(parts[-len(app_parts) :]) == app_parts:
                return app
    config_dir = posixpath.dirname(definition.file)
    owners = [
        a for a in candidates if a.path != "." and (config_dir + "/").startswith(a.path + "/")
    ]
    if owners:
        return max(owners, key=lambda a: len(a.path))
    if len(candidates) == 1:
        return candidates[0]
    return None


def effective_argv(argv: list[str]) -> list[str]:
    """O comando que de fato roda: ``sh -c 'prep && exec uvicorn ...'`` → ``uvicorn ...``."""
    if len(argv) >= 3 and PurePosixPath(argv[0]).name in ("sh", "bash") and "-c" in argv:
        script = argv[argv.index("-c") + 1] if argv.index("-c") + 1 < len(argv) else ""
        segments = re.split(r"&&|;|\n", script.replace("\\\n", " "))
        filled = [segment.strip() for segment in segments if segment.strip()]
        last = filled[-1] if filled else ""
        last = re.sub(r"^exec\s+", "", last)
        try:
            return shlex.split(last) or argv
        except ValueError:
            return argv
    return argv


def _kind(program: str, argv: list[str], app: Application) -> tuple[str, ProcessKind]:
    """Nome e tipo do processo no Dockerizer."""
    if program == "celery":
        if "beat" in argv:
            return "beat", ProcessKind.SCHEDULER
        if "worker" in argv:
            return "worker", ProcessKind.WORKER
    if program in WEB_SERVERS or (program == "manage.py" and "runserver" in argv):
        return "web", ProcessKind.WEB
    if program == "flask" and "run" in argv:
        return "web", ProcessKind.WEB
    if app.language is Language.NODE and program in NODE_EXECUTABLES | {"node"}:
        text = " ".join(argv)
        if not re.search(r"worker|queue|cron|job|consumer", text):
            return "web", ProcessKind.WEB
    return "", ProcessKind.WORKER


def _celery_app(argv: list[str]) -> str | None:
    if program_of(argv) != "celery":
        return None
    for flag in ("-A", "--app"):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            return argv[argv.index(flag) + 1]
    for arg in argv:
        if arg.startswith("--app="):
            return arg.split("=", 1)[1]
    return None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "processo"


def apply_process_definitions(
    analysis: ProjectAnalysis, definitions: list[ProcessDefinition]
) -> None:
    for definition in definitions:
        program = program_of(_strip_env_prefix(list(definition.argv), dict(definition.env)))
        if program in ("nginx", "caddy", "redis-server", "postgres", "mysqld"):
            analysis.notes.append(
                f"{definition.source}: '{program}' não é processo da aplicação; ignorado."
            )
            continue
        app = (
            analysis.app(definition.app)
            if definition.app
            else _match_app(definition, analysis.applications)
        )
        if app is None:
            analysis.notes.append(
                f"{definition.source}: não foi possível associar a uma aplicação; ignorado."
            )
            continue
        argv, port = normalize(definition)
        if not argv:
            continue
        app.warnings.extend(definition.warnings)
        effective = effective_argv(argv)
        name, kind = _kind(program_of(effective), effective, app)
        command = Command(argv=argv, source=definition.source)
        if definition.dev:
            target = app.process(name) if name else None
            if kind is ProcessKind.WEB:
                target = next(
                    (p for p in app.processes if p.kind in (ProcessKind.WEB, ProcessKind.STATIC)),
                    None,
                )
            if target is not None:
                target.dev_command = command
                if port:
                    target.dev_port = port
            continue
        existing = app.process(name) if name else None
        if (
            existing is not None
            and existing.command
            and existing.command.source.startswith("Procfile")
        ):
            continue  # o Procfile é declarado explicitamente para containers/PaaS: prevalece
        if kind is ProcessKind.WEB:
            process = existing or app.process("web")
            if process is None or process.kind is ProcessKind.STATIC:
                process = Process(name="web", kind=ProcessKind.WEB)
                app.processes = [p for p in app.processes if p.kind is not ProcessKind.STATIC]
                app.processes.insert(0, process)
            process.command = command
            process.candidates = []
            if port:
                process.port = port
                if process.dev_port is None:
                    process.dev_port = port
        elif existing is not None:
            existing.command = command
            existing.candidates = []
            celery_target = _celery_app(effective)
            if celery_target:
                existing.celery_app = celery_target  # healthcheck "inspect ping" usa o app real
        else:
            base = _slug(definition.name)
            unique = base
            counter = 2
            while app.process(unique):
                unique = f"{base}-{counter}"
                counter += 1
            celery_app = _celery_app(effective)
            app.processes.append(
                Process(name=name or unique, kind=kind, command=command, celery_app=celery_app)
            )
            if program_of(effective) == "celery" and Framework.CELERY not in app.frameworks:
                app.frameworks.append(Framework.CELERY)
        clustered = any("modo cluster" in w for w in definition.warnings)
        if definition.instances and definition.instances > 1 and not clustered:
            app.warnings.append(
                f"{definition.source} roda {definition.instances} instâncias; no Compose há uma "
                "por serviço (use 'docker compose up --scale' ou workers do servidor)."
            )
        app.evidence.append(f"Comando de produção lido de {definition.source}")
