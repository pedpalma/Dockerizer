"""Analisador de aplicações Python (FastAPI, Flask, Django, Celery, Alembic; pip, Poetry, uv).

Regras determinísticas: comandos de inicialização só são considerados *detectados* quando há
evidência explícita (Procfile, instância do framework, ``manage.py``). Caso contrário, a
aplicação fica com o comando pendente e uma pergunta é gerada pelo orquestrador.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from dockerizer.analyzers.env import python_env_refs
from dockerizer.constants import DEFAULT_PYTHON_VERSION
from dockerizer.models.architecture import (
    Application,
    AppRole,
    Command,
    Confidence,
    Framework,
    Migration,
    PackageManager,
    Process,
    ProcessKind,
)
from dockerizer.scanner.discovery import ProjectFiles, join

REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?")
SKIP_SOURCE_PARTS = frozenset({"tests", "test", "migrations", "alembic", "docs", "node_modules"})
PREFERRED_MODULES = ("main", "app", "asgi", "wsgi", "server", "api", "application", "run")
INSTANCE_RE = {
    "FastAPI": re.compile(r"^([A-Za-z_]\w*)\s*(?::\s*[\w.]+\s*)?=\s*(?:fastapi\.)?FastAPI\(", re.M),
    "Flask": re.compile(r"^([A-Za-z_]\w*)\s*(?::\s*[\w.]+\s*)?=\s*(?:flask\.)?Flask\(", re.M),
    "Celery": re.compile(r"^([A-Za-z_]\w*)\s*(?::\s*[\w.]+\s*)?=\s*(?:celery\.)?Celery\(", re.M),
}
FACTORY_RE = re.compile(r"^def\s+(create_app|make_app|get_app)\s*\(", re.M)
BEAT_RE = re.compile(r"beat_schedule|CELERY_BEAT_SCHEDULE|django_celery_beat|crontab\(")
DJANGO_SETTINGS_RE = re.compile(r"DJANGO_SETTINGS_MODULE[\"']\s*,\s*[\"']([\w.]+)[\"']")
PORT_PATTERNS = (
    re.compile(r"--port[= ](\d{2,5})"),
    re.compile(r"(?:--bind|-b)[= ]\S*:(\d{2,5})"),
    re.compile(r"runserver\s+(?:[\d.]+:)?(\d{2,5})"),
    re.compile(r"\s-p\s+(\d{2,5})"),
)
REQUIREMENTS_MAIN = (
    "requirements.txt",
    "requirements/production.txt",
    "requirements/prod.txt",
    "requirements/base.txt",
    "requirements/requirements.txt",
)
REQUIREMENTS_DEV = (
    "requirements-dev.txt",
    "requirements_dev.txt",
    "dev-requirements.txt",
    "requirements/dev.txt",
    "requirements/development.txt",
    "requirements/local.txt",
)
SHELL_CHARS = re.compile(r"[;&|<>`$]")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_requirement(spec: str) -> list[str]:
    match = REQ_NAME_RE.match(spec)
    if not match:
        return []
    name = normalize(match.group(1))
    tokens = [name]
    if match.group(2):
        extras = [normalize(e) for e in match.group(2)[1:-1].split(",") if e.strip()]
        tokens.extend(f"{name}[{extra}]" for extra in extras)
    return tokens


@dataclass
class _Requirements:
    names: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def _read_requirements(
    files: ProjectFiles, app_dir: str, rel: str, acc: _Requirements, depth: int = 0
) -> None:
    if rel in acc.files or depth > 5 or not files.has(join(app_dir, rel)):
        return
    acc.files.append(rel)
    base = PurePosixPath(rel).parent
    for raw in files.read(join(app_dir, rel)).splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement ", "-c ", "--constraint ")):
            included = line.split(maxsplit=1)[1].strip()
            target = (base / included).as_posix() if str(base) != "." else included
            _read_requirements(files, app_dir, target, acc, depth + 1)
            continue
        if line.startswith("-"):
            continue
        acc.names.extend(_parse_requirement(line))


def _load_toml(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}


def _poetry_deps(table: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for name, spec in table.items():
        if name.lower() == "python":
            continue
        names.append(normalize(name))
        if isinstance(spec, dict):
            names.extend(f"{normalize(name)}[{normalize(e)}]" for e in spec.get("extras", []))
    return names


@dataclass
class _PyprojectInfo:
    main: list[str] = field(default_factory=list)
    dev: list[str] = field(default_factory=list)
    raw_main: list[str] = field(default_factory=list)
    requires_python: str | None = None
    has_build_system: bool = False
    is_poetry: bool = False
    scripts: dict[str, str] = field(default_factory=dict)


def _pyproject_info(data: dict[str, Any]) -> _PyprojectInfo:
    info = _PyprojectInfo()
    project = data.get("project", {})
    tool = data.get("tool", {})
    poetry = tool.get("poetry", {})
    info.has_build_system = "build-system" in data
    info.requires_python = project.get("requires-python")
    info.scripts = {str(k): str(v) for k, v in project.get("scripts", {}).items()}
    for spec in project.get("dependencies", []):
        info.raw_main.append(str(spec))
        info.main.extend(_parse_requirement(str(spec)))
    for group in data.get("dependency-groups", {}).values():
        for spec in group:
            if isinstance(spec, str):
                info.dev.extend(_parse_requirement(spec))
    for group in project.get("optional-dependencies", {}).values():
        for spec in group:
            info.dev.extend(_parse_requirement(str(spec)))
    if poetry:
        info.is_poetry = bool(poetry.get("dependencies") or poetry.get("group"))
        deps = poetry.get("dependencies", {})
        info.main.extend(_poetry_deps(deps))
        if isinstance(deps.get("python"), str) and not info.requires_python:
            info.requires_python = deps["python"]
        info.dev.extend(_poetry_deps(poetry.get("dev-dependencies", {})))
        for group in poetry.get("group", {}).values():
            info.dev.extend(_poetry_deps(group.get("dependencies", {})))
        info.scripts.update({str(k): str(v) for k, v in poetry.get("scripts", {}).items()})
    return info


def _minor(version: str) -> tuple[int, int] | None:
    match = re.match(r"(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2))) if match else None


def choose_python_version(spec: str | None, pinned: str | None = None) -> str:
    """Escolhe a versão ``X.Y`` da imagem base respeitando restrições do projeto."""
    if pinned:
        minor = _minor(pinned)
        if minor:
            return f"{minor[0]}.{minor[1]}"
    default = _minor(DEFAULT_PYTHON_VERSION)
    assert default is not None  # noqa: S101
    if not spec:
        return DEFAULT_PYTHON_VERSION
    exact = re.search(r"==\s*(\d+\.\d+)", spec)
    if exact:
        return exact.group(1)
    lowers = [_minor(v) for v in re.findall(r"(?:>=|\^|~=|~)\s*(\d+\.\d+)", spec)]
    uppers = [_minor(v) for v in re.findall(r"<\s*(\d+\.\d+)", spec)]
    lower = max((v for v in lowers if v), default=None)
    upper = min((v for v in uppers if v), default=None)
    if "^" in spec and lower and not upper:
        upper = (lower[0] + 1, 0)
    if (lower is None or default >= lower) and (upper is None or default < upper):
        return DEFAULT_PYTHON_VERSION
    if lower is not None:
        return f"{lower[0]}.{lower[1]}"
    if upper is not None:
        return f"{upper[0]}.{upper[1] - 1}"
    return DEFAULT_PYTHON_VERSION


def _module_of(rel_in_app: str, python_path: str | None) -> str:
    path = PurePosixPath(rel_in_app)
    if python_path and path.parts and path.parts[0] == python_path:
        path = PurePosixPath(*path.parts[1:])
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass
class _Instance:
    module: str
    attr: str
    rel: str
    factory: bool = False

    @property
    def target(self) -> str:
        return f"{self.module}:{self.attr}()" if self.factory else f"{self.module}:{self.attr}"


def _rank(instance: _Instance) -> tuple[int, int, str]:
    stem = instance.module.rsplit(".", 1)[-1]
    preferred = PREFERRED_MODULES.index(stem) if stem in PREFERRED_MODULES else 99
    return (preferred, instance.module.count("."), instance.module)


def _source_files(files: ProjectFiles, app_dir: str) -> list[str]:
    prefix_len = 0 if app_dir == "." else len(app_dir) + 1
    sources = []
    for rel in files.under(app_dir, (".py",)):
        inner = rel[prefix_len:]
        if any(part in SKIP_SOURCE_PARTS for part in PurePosixPath(inner).parts[:-1]):
            continue
        sources.append(inner)
    return sources


def _port_from(argv: list[str], default: int | None) -> int | None:
    text = " " + " ".join(argv)
    for pattern in PORT_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return default


def _procfile_command(raw: str, port: int) -> list[str]:
    cmd = re.sub(r"\$\{?PORT\}?", str(port), raw.strip())
    if SHELL_CHARS.search(cmd):
        return ["sh", "-c", cmd]
    return shlex.split(cmd)


def _parse_procfile(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.+)$", line.strip())
        if match:
            entries[match.group(1)] = match.group(2)
    return entries


class PythonAnalyzer:
    def __init__(self, files: ProjectFiles, app_dir: str, name: str) -> None:
        self.files = files
        self.app_dir = app_dir
        self.name = name

    def _rel(self, name: str) -> str:
        return join(self.app_dir, name)

    def _has(self, name: str) -> bool:
        return self.files.has(self._rel(name))

    def _read(self, name: str) -> str:
        return self.files.read(self._rel(name))

    def analyze(self) -> Application | None:
        pyproject = (
            _pyproject_info(_load_toml(self._read("pyproject.toml")))
            if self._has("pyproject.toml")
            else _PyprojectInfo()
        )
        app = self._dependencies(pyproject)
        if app is None:
            return None
        sources = _source_files(self.files, self.app_dir)
        texts = {rel: self._read(rel) for rel in sources}
        if any(rel.startswith("src/") for rel in sources) and not self._has("src/__init__.py"):
            app.python_path = "src"
        self._frameworks(app)
        for text in texts.values():
            app.env_vars.extend(python_env_refs(text))
        app.env_vars = sorted(set(app.env_vars))
        self._processes(app, texts, pyproject)
        self._migrations(app)
        if app.has_framework(Framework.DJANGO):
            app.warnings.extend(django_production_warnings(app, texts))
        app.role = AppRole.BACKEND if app.process("web") else AppRole.WORKER
        return app

    # -- dependências e gerenciador -------------------------------------------------------
    def _dependencies(self, pyproject: _PyprojectInfo) -> Application | None:
        evidence: list[str] = []
        manager: PackageManager
        dep_files: list[str] = []
        dev_files: list[str] = []
        inline: list[str] = []
        deps: list[str] = list(pyproject.main)
        if self._has("uv.lock") and self._has("pyproject.toml"):
            manager = PackageManager.UV
            dep_files = ["pyproject.toml", "uv.lock"]
            evidence.append("uv.lock encontrado")
        elif self._has("poetry.lock") or pyproject.is_poetry:
            manager = PackageManager.POETRY
            dep_files = ["pyproject.toml"] + (["poetry.lock"] if self._has("poetry.lock") else [])
            evidence.append("Projeto Poetry (pyproject.toml/poetry.lock)")
        else:
            manager = PackageManager.PIP
            main_req = next((r for r in REQUIREMENTS_MAIN if self._has(r)), None)
            if main_req:
                acc = _Requirements()
                _read_requirements(self.files, self.app_dir, main_req, acc)
                deps.extend(acc.names)
                dep_files = acc.files
                evidence.append(f"{main_req} encontrado")
                dev_req = next((r for r in REQUIREMENTS_DEV if self._has(r)), None)
                if dev_req:
                    dev_acc = _Requirements(files=list(acc.files))
                    _read_requirements(self.files, self.app_dir, dev_req, dev_acc)
                    dev_files = [f for f in dev_acc.files if f not in acc.files]
            elif pyproject.main or pyproject.has_build_system:
                dep_files = ["pyproject.toml"]
                inline = pyproject.raw_main if not pyproject.has_build_system else []
                evidence.append("Dependências declaradas em pyproject.toml ([project])")
            elif self._has("setup.py"):
                pyproject.has_build_system = True
                evidence.append("setup.py encontrado")
            elif self._has("manage.py"):
                evidence.append("manage.py encontrado sem arquivo de dependências")
            else:
                return None

        pinned = self._read(".python-version").strip() if self._has(".python-version") else None
        if not pinned and self._has("runtime.txt"):
            pinned = self._read("runtime.txt").strip().removeprefix("python-")
        app = Application(
            name=self.name,
            path=self.app_dir,
            language="python",
            language_version=choose_python_version(pyproject.requires_python, pinned or None),
            package_manager=manager,
            role=AppRole.BACKEND,
            dependencies=sorted(set(deps)),
            evidence=evidence,
            dependency_files=dep_files,
            dev_dependency_files=dev_files,
            inline_dependencies=inline,
            has_build_system=pyproject.has_build_system,
        )
        if self._has("manage.py") and "django" not in app.dependencies:
            app.warnings.append("manage.py encontrado, mas Django não está nas dependências.")
        return app

    def _frameworks(self, app: Application) -> None:
        deps = set(app.dependencies)
        mapping = {
            "fastapi": Framework.FASTAPI,
            "flask": Framework.FLASK,
            "django": Framework.DJANGO,
            "celery": Framework.CELERY,
            "alembic": Framework.ALEMBIC,
        }
        app.frameworks = [fw for dep, fw in mapping.items() if dep in deps]
        if self._has("manage.py") and Framework.DJANGO not in app.frameworks:
            app.frameworks.append(Framework.DJANGO)

    # -- processos ------------------------------------------------------------------------
    def _processes(
        self, app: Application, texts: dict[str, str], pyproject: _PyprojectInfo
    ) -> None:
        procfile = _parse_procfile(self._read("Procfile")) if self._has("Procfile") else {}
        web = self._web_process(app, texts)
        if "web" in procfile:
            port = web.port if web and web.port else 8000
            argv = _procfile_command(procfile["web"], port)
            prod = Command(argv=argv, source="Procfile (web)")
            web = web or Process(name="web", kind=ProcessKind.WEB)
            web.command = prod
            web.port = _port_from(argv, port)
            app.evidence.append("Procfile define o processo web")
        if web is None and not app.has_framework(Framework.CELERY):
            web = self._generic_candidates(app, texts, pyproject)
        if web is not None:
            app.processes.append(web)
        self._celery_processes(app, texts, procfile)
        if "release" in procfile and app.migration is None:
            argv = _procfile_command(procfile["release"], 8000)
            app.migration = Migration(tool="procfile", command=argv, source="Procfile (release)")

    def _find_instances(
        self, texts: dict[str, str], cls: str, python_path: str | None
    ) -> list[_Instance]:
        found: list[_Instance] = []
        for rel, text in texts.items():
            for match in INSTANCE_RE[cls].finditer(text):
                found.append(_Instance(_module_of(rel, python_path), match.group(1), rel))
        if not found and cls in ("FastAPI", "Flask"):
            for rel, text in texts.items():
                if f"{cls}(" in text:
                    factory = FACTORY_RE.search(text)
                    if factory:
                        module = _module_of(rel, python_path)
                        found.append(_Instance(module, factory.group(1), rel, factory=True))
        return sorted(found, key=_rank)

    def _pick(
        self, app: Application, instances: list[_Instance], label: str
    ) -> tuple[_Instance | None, list[_Instance]]:
        if not instances:
            return None, []
        if len(instances) == 1:
            return instances[0], []
        best, second = _rank(instances[0]), _rank(instances[1])
        if best[0] < second[0] or (best[0] == second[0] and best[1] < second[1]):
            return instances[0], instances[1:]
        app.warnings.append(
            f"Várias instâncias {label} encontradas: "
            + ", ".join(i.target for i in instances)
            + ". Confirme qual deve ser utilizada."
        )
        return None, instances

    def _web_process(self, app: Application, texts: dict[str, str]) -> Process | None:
        deps = set(app.dependencies)
        has_gunicorn = "gunicorn" in deps
        has_uvicorn = (
            "uvicorn" in deps or "fastapi[standard]" in deps or "uvicorn[standard]" in deps
        )
        if app.has_framework(Framework.DJANGO) and self._has("manage.py"):
            return self._django_process(app, has_gunicorn, has_uvicorn)
        if app.has_framework(Framework.FASTAPI):
            instance, others = self._pick(
                app, self._find_instances(texts, "FastAPI", app.python_path), "FastAPI"
            )
            process = Process(name="web", kind=ProcessKind.WEB, port=8000)
            alternatives = [instance] if instance else []
            alternatives += others
            process.candidates = [
                Command(
                    argv=["uvicorn", i.target, "--host", "0.0.0.0", "--port", "8000"],
                    source=f"FastAPI em {i.rel}",
                    confidence=Confidence.SUGGESTED,
                )
                for i in alternatives
            ]
            if instance is None:
                return process
            factory = ["--factory"] if instance.factory else []
            target = f"{instance.module}:{instance.attr}"
            base = ["uvicorn", target, *factory, "--host", "0.0.0.0", "--port", "8000"]
            confidence = Confidence.DETECTED if has_uvicorn else Confidence.SUGGESTED
            if not has_uvicorn:
                app.warnings.append(
                    "FastAPI detectado, mas uvicorn não está nas dependências; "
                    "o comando sugerido requer uvicorn instalado."
                )
            process.command = Command(
                argv=base, source=f"FastAPI em {instance.rel}", confidence=confidence
            )
            process.dev_command = Command(
                argv=[*base, "--reload"], source=f"FastAPI em {instance.rel}", confidence=confidence
            )
            app.evidence.append(f"Instância FastAPI em {instance.rel}")
            return process
        if app.has_framework(Framework.FLASK):
            instance, _ = self._pick(
                app, self._find_instances(texts, "Flask", app.python_path), "Flask"
            )
            process = Process(name="web", kind=ProcessKind.WEB)
            if instance is None:
                return process
            target = instance.target
            dev = Command(
                argv=[
                    "flask",
                    "--app",
                    target,
                    "run",
                    "--debug",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "5000",
                ],
                source=f"Flask em {instance.rel}",
            )
            if has_gunicorn:
                process.command = Command(
                    argv=["gunicorn", "--bind", "0.0.0.0:8000", target],
                    source=f"Flask em {instance.rel} + gunicorn",
                )
                process.port = 8000
            else:
                process.command = Command(
                    argv=["flask", "--app", target, "run", "--host", "0.0.0.0", "--port", "5000"],
                    source=f"Flask em {instance.rel}",
                )
                process.port = 5000
                app.warnings.append(
                    "Flask sem gunicorn: o servidor de desenvolvimento do Flask será usado "
                    "também em produção. Recomenda-se adicionar gunicorn."
                )
            process.dev_command = dev
            process.dev_port = 5000
            app.evidence.append(f"Instância Flask em {instance.rel}")
            return process
        return None

    def _django_process(self, app: Application, gunicorn: bool, uvicorn: bool) -> Process:
        match = DJANGO_SETTINGS_RE.search(self._read("manage.py"))
        process = Process(name="web", kind=ProcessKind.WEB, port=8000)
        runserver = ["python", "manage.py", "runserver", "0.0.0.0:8000"]
        process.dev_command = Command(argv=runserver, source="manage.py")
        if not match:
            app.warnings.append("DJANGO_SETTINGS_MODULE não encontrado em manage.py.")
            process.command = Command(argv=runserver, source="manage.py")
            return process
        package = match.group(1).split(".")[0]
        app.evidence.append(f"Django com settings em {match.group(1)}")
        if gunicorn and self._has(f"{package}/wsgi.py"):
            process.command = Command(
                argv=["gunicorn", f"{package}.wsgi:application", "--bind", "0.0.0.0:8000"],
                source=f"Django ({package}/wsgi.py) + gunicorn",
            )
        elif uvicorn and self._has(f"{package}/asgi.py"):
            process.command = Command(
                argv=[
                    "uvicorn",
                    f"{package}.asgi:application",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8000",
                ],
                source=f"Django ({package}/asgi.py) + uvicorn",
            )
        else:
            process.command = Command(argv=runserver, source="manage.py")
            app.warnings.append(
                "Django sem gunicorn/uvicorn: o runserver (servidor de desenvolvimento) será "
                "usado em produção. Recomenda-se adicionar gunicorn."
            )
        return process

    def _generic_candidates(
        self, app: Application, texts: dict[str, str], pyproject: _PyprojectInfo
    ) -> Process | None:
        candidates: list[Command] = []
        for script in pyproject.scripts:
            candidates.append(
                Command(
                    argv=[script],
                    source="pyproject.toml [scripts]",
                    confidence=Confidence.SUGGESTED,
                )
            )
        for name in ("main.py", "app.py", "run.py", "server.py"):
            if name in texts and "__main__" in texts[name]:
                candidates.append(
                    Command(argv=["python", name], source=name, confidence=Confidence.SUGGESTED)
                )
        if not candidates and not texts:
            return None
        app.warnings.append(
            "Nenhum framework web reconhecido; o comando de inicialização precisa ser confirmado."
        )
        return Process(name="web", kind=ProcessKind.WEB, candidates=candidates)

    def _celery_processes(
        self, app: Application, texts: dict[str, str], procfile: dict[str, str]
    ) -> None:
        if not app.has_framework(Framework.CELERY):
            return
        instance, _ = self._pick(
            app, self._find_instances(texts, "Celery", app.python_path), "Celery"
        )
        worker = Process(name="worker", kind=ProcessKind.WORKER)
        for key in ("worker", "celery"):
            if key in procfile:
                worker.command = Command(
                    argv=_procfile_command(procfile[key], 8000), source=f"Procfile ({key})"
                )
        if instance is not None:
            target = instance.module if instance.attr in ("app", "celery") else instance.target
            worker.celery_app = target
            if worker.command is None:
                worker.command = Command(
                    argv=["celery", "-A", target, "worker", "--loglevel=INFO"],
                    source=f"Celery em {instance.rel}",
                )
            app.evidence.append(f"Instância Celery em {instance.rel}")
            if any(BEAT_RE.search(t) for t in texts.values()) or "beat" in procfile:
                beat_cmd = (
                    _procfile_command(procfile["beat"], 8000)
                    if "beat" in procfile
                    else ["celery", "-A", target, "beat", "--loglevel=INFO"]
                )
                app.processes.append(
                    Process(
                        name="beat",
                        kind=ProcessKind.SCHEDULER,
                        command=Command(argv=beat_cmd, source="Agendamento Celery (beat)"),
                        celery_app=target,
                    )
                )
        elif worker.command is None:
            app.warnings.append("Celery está nas dependências, mas a instância não foi encontrada.")
        app.processes.insert(1 if app.process("web") else 0, worker)

    def _migrations(self, app: Application) -> None:
        if app.migration is not None:
            return
        if app.has_framework(Framework.DJANGO) and self._has("manage.py"):
            app.migration = Migration(
                tool="django",
                command=["python", "manage.py", "migrate", "--noinput"],
                source="manage.py",
            )
        elif self._has("alembic.ini"):
            if "alembic" not in app.dependencies:
                app.warnings.append(
                    "alembic.ini encontrado, mas alembic não está nas dependências."
                )
            app.migration = Migration(
                tool="alembic", command=["alembic", "upgrade", "head"], source="alembic.ini"
            )
            if Framework.ALEMBIC not in app.frameworks:
                app.frameworks.append(Framework.ALEMBIC)


DJANGO_DEBUG_TRUE_RE = re.compile(r"^DEBUG\s*=\s*True\b", re.MULTILINE)
DJANGO_EMPTY_HOSTS_RE = re.compile(r"^ALLOWED_HOSTS\s*=\s*\[\s*\]", re.MULTILINE)
DJANGO_STATIC_ROOT_RE = re.compile(r"^STATIC_ROOT\s*=", re.MULTILINE)


def django_production_warnings(app: Application, texts: dict[str, str]) -> list[str]:
    """Configurações do Django que quebram ou expõem a aplicação num container de produção.

    Nada é alterado: só o próprio projeto sabe os hosts e o destino dos arquivos estáticos.
    """
    settings = {rel: text for rel, text in texts.items() if "settings" in rel}
    if not settings:
        return []
    joined = "\n".join(settings.values())
    where = ", ".join(sorted(settings))
    warnings = []
    if DJANGO_DEBUG_TRUE_RE.search(joined):
        warnings.append(
            f"DEBUG = True fixo em {where}: em produção, leia-o de uma variável de ambiente "
            "(páginas de erro do Django expõem configurações e código)."
        )
    if DJANGO_EMPTY_HOSTS_RE.search(joined):
        warnings.append(
            f"ALLOWED_HOSTS = [] em {where}: com DEBUG desligado o Django recusa todas as "
            "requisições (Bad Request 400). Inclua o domínio e, para o healthcheck, 'localhost'."
        )
    has_whitenoise = "whitenoise" in app.dependencies
    if not DJANGO_STATIC_ROOT_RE.search(joined):
        warnings.append(
            f"STATIC_ROOT não está definido em {where}: o collectstatic não roda no build e "
            "CSS/JS do admin não serão servidos pelo gunicorn."
        )
    elif not has_whitenoise:
        warnings.append(
            "collectstatic não é executado pelo Dockerizer e o gunicorn não serve arquivos "
            "estáticos: use whitenoise ou sirva STATIC_ROOT pelo nginx (rode "
            "'python manage.py collectstatic' no build do seu Dockerfile)."
        )
    return warnings
