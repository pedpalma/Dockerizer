"""Geração de Dockerfile e .dockerignore por aplicação."""

from __future__ import annotations

from pathlib import PurePosixPath

from dockerizer.constants import NGINX_IMAGE, UV_IMAGE
from dockerizer.generators.templating import render
from dockerizer.models.architecture import (
    Application,
    Command,
    Language,
    Mode,
    PackageManager,
    Process,
    ProcessKind,
)

MODE_LABELS = {Mode.PRODUCTION: "produção", Mode.DEVELOPMENT: "desenvolvimento"}

NODE_INSTALL = {
    (PackageManager.NPM, True): "npm ci",
    (PackageManager.NPM, False): "npm install",
    (PackageManager.PNPM, True): "pnpm install --frozen-lockfile",
    (PackageManager.PNPM, False): "pnpm install",
    (PackageManager.YARN, True): "yarn install --frozen-lockfile",
    (PackageManager.YARN, False): "yarn install",
}
NODE_PRUNE = {
    PackageManager.NPM: "npm prune --omit=dev",
    PackageManager.PNPM: "pnpm prune --prod",
    PackageManager.YARN: "yarn install --production --frozen-lockfile --ignore-scripts "
    "--prefer-offline",
}
NODE_CACHE = {
    PackageManager.NPM: "/root/.npm",
    PackageManager.PNPM: "/root/.local/share/pnpm/store",
    PackageManager.YARN: "/usr/local/share/.cache/yarn",
}


class GenerationError(Exception):
    """Configuração insuficiente para gerar um arquivo confiável."""


def main_process(app: Application) -> Process | None:
    """Processo que define o CMD da imagem: o web, se houver; senão o primeiro processo."""
    return app.process("web") or (app.processes[0] if app.processes else None)


def runtime_argv(command: Command, manager: PackageManager) -> list[str]:
    """Em produção, scripts pnpm/yarn são executados via ``npm run`` (incluso na imagem node),
    evitando que o corepack baixe o gerenciador durante a inicialização do container."""
    argv = command.argv
    if manager is not PackageManager.NPM and len(argv) >= 3 and argv[:2] == [manager.value, "run"]:
        extra = argv[3:]
        return ["npm", "run", argv[2], *(["--", *extra] if extra else [])]
    return argv


def render_dockerfile(app: Application, mode: Mode, existing_files: set[str]) -> str:
    process = main_process(app)
    if process is None:
        raise GenerationError(f"A aplicação '{app.name}' não possui processo executável.")
    if app.language is Language.PYTHON:
        return _python(app, mode, process)
    return _node(app, mode, process, existing_files)


def _require_command(app: Application, process: Process, mode: Mode) -> Command:
    command = process.command_for(mode)
    if command is None:
        raise GenerationError(
            f"Comando de inicialização do processo '{process.name}' da aplicação '{app.name}' "
            "não foi identificado nem confirmado."
        )
    return command


def _python(app: Application, mode: Mode, process: Process) -> str:
    command = _require_command(app, process, mode)
    dev = mode is Mode.DEVELOPMENT
    requirement_files = [f for f in app.dependency_files if f.endswith(".txt")]
    dev_requirement = app.dev_dependency_files[-1] if app.dev_dependency_files else None
    if dev and app.dev_dependency_files:
        requirement_files += [f for f in app.dev_dependency_files if f not in requirement_files]
    return render(
        "python.Dockerfile.j2",
        app=app,
        mode_label=MODE_LABELS[mode],
        dev=dev,
        pm=app.package_manager.value,
        uv_image=UV_IMAGE,
        requirement_files=requirement_files if app.package_manager is PackageManager.PIP else [],
        dev_requirement=dev_requirement,
        install_project=app.has_build_system and app.package_manager is PackageManager.PIP,
        port=process.port_for(mode) if process.kind is ProcessKind.WEB else None,
        command=command.argv,
    )


def _node(app: Application, mode: Mode, process: Process, existing: set[str]) -> str:
    dev = mode is Mode.DEVELOPMENT
    static = process.kind is ProcessKind.STATIC and not dev
    if static and not app.static_output_dir:
        raise GenerationError(f"Diretório de saída do build de '{app.name}' não definido.")
    if static and not app.build_command:
        raise GenerationError(f"A aplicação '{app.name}' não possui script de build.")
    if dev and process.kind is ProcessKind.STATIC:
        if process.dev_command is None:
            raise GenerationError(
                f"A aplicação '{app.name}' não possui script de desenvolvimento (dev/start)."
            )
        command = process.dev_command
    else:
        command = _require_command(app, process, mode)
    argv = command.argv if dev else runtime_argv(command, app.package_manager)
    locked = app.lockfile is not None
    install = NODE_INSTALL[(app.package_manager, locked)]
    if app.package_manager is PackageManager.YARN and app.yarn_berry:
        install = "yarn install --immutable" if locked else "yarn install"
    install_files = ["package.json"] + ([app.lockfile] if app.lockfile else [])
    for extra in (".npmrc", ".yarnrc.yml", ".yarnrc", "pnpm-workspace.yaml"):
        rel = str(PurePosixPath(app.path) / extra) if app.path != "." else extra
        if rel in existing:
            install_files.append(extra)
    prune = None
    if not dev and not (app.package_manager is PackageManager.YARN and app.yarn_berry):
        prune = NODE_PRUNE[app.package_manager]
    port = process.port_for(mode)
    return render(
        "node.Dockerfile.j2",
        app=app,
        mode_label=MODE_LABELS[mode],
        dev=dev,
        static=static,
        corepack=app.package_manager is not PackageManager.NPM,
        install_files=install_files,
        install_command=install,
        cache_dir=NODE_CACHE[app.package_manager],
        build_command=app.build_command,
        prune_command=prune,
        nginx_image=NGINX_IMAGE,
        port=port,
        command=argv,
    )


def render_dockerignore(app: Application, nested_apps: list[str]) -> str:
    return render(
        "dockerignore.j2",
        language=app.language.value,
        nested=nested_apps,
        exclude_build_outputs=app.build_command is not None,
    )
