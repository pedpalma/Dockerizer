"""Interface de linha de comando do Dockerizer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from dockerizer import __version__
from dockerizer.analyzers.project import analyze_project
from dockerizer.cli import i18n, ui
from dockerizer.constants import COMPOSE_FILE, TOOL_DIR, compose_project_name
from dockerizer.docker.manager import (
    DockerCommandError,
    DockerManager,
    Runner,
    UnsafeDockerOperation,
)
from dockerizer.fs.changes import ChangeAction, apply_changes, plan_changes
from dockerizer.generators.dockerfile import GenerationError
from dockerizer.generators.plan import assign_ports, build_plan, port_is_free, previous_host_ports
from dockerizer.models.architecture import Mode
from dockerizer.reporting.report import write_reports
from dockerizer.rollback.manager import (
    execute_purge,
    execute_rollback,
    plan_purge,
    plan_rollback,
)
from dockerizer.rollback.manifest import (
    DockerResources,
    Manifest,
    ManifestError,
    ManifestStore,
    RunRecord,
    new_run_id,
)
from dockerizer.validators.validation import validate

i18n.install()

# Pontos de injeção (substituídos nos testes para não depender do Docker nem das portas do host).
DOCKER_RUNNER: Runner | None = None
PORT_CHECKER: Callable[[int], bool] = port_is_free

app = typer.Typer(
    name="dockerizer",
    help="Containeriza projetos existentes com Docker e Docker Compose.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)

PathArg = Annotated[
    Path | None,
    typer.Argument(help="Caminho do projeto (padrão: diretório atual).", show_default=False),
]
PathOpt = Annotated[
    Path | None,
    typer.Option(
        "--path", help="Caminho do projeto (alternativa ao argumento).", show_default=False
    ),
]
DryRun = Annotated[
    bool, typer.Option("--dry-run", help="Mostra as operações planejadas sem executá-las.")
]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Dispensa confirmações não destrutivas.")]
Force = Annotated[
    bool,
    typer.Option(
        "--force",
        help="Permite substituir arquivos existentes ou editados (sempre com backup).",
    ),
]
Report = Annotated[
    bool, typer.Option("--report", help=f"Gera relatório da análise em {TOOL_DIR}/reports.")
]


class ExitCode:
    OK = 0
    ERROR = 1
    CONFLICT = 3


def fail(message: str, code: int = ExitCode.ERROR) -> NoReturn:
    ui.error(message)
    raise typer.Exit(code)


def project_root(path_arg: Path | None, path_opt: Path | None) -> Path:
    both = path_arg is not None and path_opt is not None
    if both and path_arg.expanduser().resolve() != path_opt.expanduser().resolve():  # type: ignore[union-attr]
        fail("Informe o caminho apenas uma vez (argumento ou --path).")
    root = (path_opt or path_arg or Path()).expanduser().resolve()
    if not root.is_dir():
        fail(f"Diretório não encontrado: {root}")
    return root


def load_manifest(store: ManifestStore) -> Manifest | None:
    try:
        return store.load()
    except ManifestError as exc:
        fail(str(exc))


# Arquivos do usuário que o Dockerizer só complementa (não substitui).
APPENDED_FILES = frozenset({".env", ".gitignore"})


def generated_paths(manifest: Manifest | None) -> list[str]:
    """Arquivos cujo conteúdo é do Dockerizer (ignorados como evidência na análise).

    Um ``.env`` preexistente que apenas foi complementado continua sendo do usuário: as URLs
    dele seguem valendo como evidência.
    """
    if manifest is None:
        return []
    return [
        path
        for path, record in manifest.files.items()
        if not (record.action == "modified" and path in APPENDED_FILES)
    ]


def require_manifest(store: ManifestStore) -> Manifest:
    manifest = load_manifest(store)
    if manifest is None:
        fail(
            f"Nenhuma configuração do Dockerizer encontrada em {store.root}. "
            "Execute 'dockerizer setup' primeiro."
        )
    return manifest


def docker_for(
    root: Path, manifest: Manifest | None, project: str, dry_run: bool = False
) -> DockerManager:
    resources = manifest.docker if manifest else None
    return DockerManager(
        root,
        resources.compose_project if resources else compose_project_name(project),
        resources.compose_file if resources else COMPOSE_FILE,
        runner=DOCKER_RUNNER,
        dry_run=dry_run,
        on_command=ui.show_command,
    )


def require_docker(docker: DockerManager) -> None:
    if not docker.cli_available():
        fail("Docker CLI com o plugin Compose não encontrado. Instale o Docker e tente novamente.")
    if not docker.daemon_running():
        fail("O daemon do Docker não está em execução (ou sem permissão de acesso).")


def version_callback(value: bool) -> None:
    if value:
        ui.info(f"dockerizer {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version", callback=version_callback, is_eager=True, help="Mostra a versão."
        ),
    ] = None,
) -> None:
    """Containeriza projetos existentes com Docker e Docker Compose."""


@app.command()
def analyze(path: PathArg = None, path_opt: PathOpt = None, report: Report = False) -> None:
    """Analisa o projeto sem modificá-lo."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = load_manifest(store)
    analysis = analyze_project(root, generated_paths(manifest))
    ui.show_analysis(analysis)
    if report:
        paths = write_reports(store, analysis)
        ui.success(f"Relatório gerado: {paths.markdown.relative_to(root)} e {paths.json.name}")


@app.command()
def setup(
    path: PathArg = None,
    path_opt: PathOpt = None,
    dry_run: DryRun = False,
    yes: Yes = False,
    force: Force = False,
    build: Annotated[bool, typer.Option("--build", help="Constrói as imagens.")] = False,
    start: Annotated[bool, typer.Option("--start", help="Inicia os serviços.")] = False,
    dev: Annotated[
        bool,
        typer.Option(
            "--dev/--production",
            help="Ambiente de desenvolvimento (código montado e servidores com reload) ou "
            "produção.",
        ),
    ] = False,
    report: Report = False,
) -> None:
    """Analisa o projeto, gera os arquivos Docker e valida a configuração."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = load_manifest(store)
    mode = Mode.DEVELOPMENT if dev else Mode.PRODUCTION

    analysis = analyze_project(root, generated_paths(manifest))
    ui.show_analysis(analysis)
    if not analysis.applications:
        fail("Nada a containerizar.")
    answers = dict(manifest.answers) if manifest else {}
    unresolved = ui.resolve_questions(analysis, yes, answers)
    if unresolved:
        fail(
            "Informações obrigatórias sem confirmação: "
            + "; ".join(q.prompt for q in unresolved)
            + ". Execute sem --yes para respondê-las."
        )

    previous = previous_host_ports(root) if manifest and COMPOSE_FILE in manifest.files else {}
    host_ports = ui.resolve_ports(assign_ports(analysis, mode, previous, PORT_CHECKER), yes)
    if host_ports is None:
        raise typer.Exit(ExitCode.ERROR)
    try:
        plan = build_plan(analysis, root, mode, host_ports, store.load_credentials())
    except GenerationError as exc:
        fail(str(exc))
    changes = plan_changes(root, plan.files, manifest, force)
    ui.show_changes(changes)
    for warning in plan.warnings:
        ui.warn(warning)

    effective = {
        c.file.path: c.file.content
        for c in changes
        if c.writes or c.action is ChangeAction.UNCHANGED
    }
    static = validate(root, overrides=effective)
    conflicts = [c for c in changes if c.action is ChangeAction.CONFLICT]

    if dry_run:
        if conflicts:
            ui.warn(
                f"{len(conflicts)} conflito(s) impediriam a geração; a validação foi omitida "
                "porque o resultado ficaria incompleto. Revise os arquivos ou use --force."
            )
        else:
            ui.show_validation(static)
        manager = docker_for(root, None, analysis.project_name, dry_run=True)
        if build:
            manager.build()
        if start:
            manager.up()
        if report:
            ui.info("[dim]Relatório não gravado em dry-run.[/dim]")
        ui.info(
            "[bold]Dry-run:[/bold] nenhum arquivo foi alterado e nenhum comando Docker executado."
        )
        if conflicts:
            raise typer.Exit(ExitCode.CONFLICT)
        return

    if conflicts:
        fail(
            f"{len(conflicts)} conflito(s) impedem a geração. Nenhum arquivo foi alterado. "
            "Revise os arquivos indicados ou use --force (com backup automático).",
            ExitCode.CONFLICT,
        )
    if not static.ok:
        ui.show_validation(static)
        fail("A configuração gerada não passou na validação; nenhum arquivo foi alterado.")

    pending = [c for c in changes if c.writes]
    if pending and not yes and not ui.confirm(f"Aplicar {len(pending)} alteração(ões)?", True):
        fail("Operação cancelada; nenhum arquivo foi alterado.")

    run_id = new_run_id()
    manifest = manifest or Manifest(project_name=analysis.project_name)
    manifest.answers = answers
    run = RunRecord(id=run_id, command="setup", mode=mode.value)
    manifest.runs.append(run)
    manifest.mode = mode.value
    started = manifest.docker.started if manifest.docker else False
    manifest.docker = DockerResources(
        compose_project=compose_project_name(analysis.project_name),
        compose_file=COMPOSE_FILE,
        services=plan.compose.services,
        images=plan.compose.images,
        volumes=plan.compose.volumes,
        networks=plan.compose.networks,
        started=started,
    )
    store.save(manifest)
    store.save_credentials(plan.infra_values)
    written = apply_changes(root, changes, store, manifest, run_id)
    if written:
        ui.success(f"{len(written)} arquivo(s) gravado(s). Alterações registradas em {TOOL_DIR}/.")
    else:
        ui.success("Arquivos já estavam atualizados.")

    docker = docker_for(root, manifest, analysis.project_name)
    own_ports = {binding.host_port for binding in plan.compose.ports}
    validation = validate(root, docker=docker, own_ports=own_ports)
    ui.show_validation(validation)
    if report:
        paths = write_reports(store, analysis, changes, validation)
        ui.success(f"Relatório gerado: {paths.markdown.relative_to(root)}")
    if not validation.ok:
        run.status = "validacao_falhou"
        store.save(manifest)
        fail("Validação falhou. Corrija os problemas ou use 'dockerizer rollback' para desfazer.")

    if build or start:
        require_docker(docker)
    if build:
        ui.info("[bold]Construindo imagens...[/bold]")
        result = docker.build()
        if not result.ok:
            run.status = "falha_build"
            store.save(manifest)
            fail(
                "Falha na construção das imagens. Os arquivos gerados foram mantidos para "
                "correção; use 'dockerizer rollback' para desfazer."
            )
        ui.success("Imagens construídas.")
    if start:
        _start(docker, store, manifest, run)
    run.status = "concluido"
    store.save(manifest)
    if not start:
        ui.info("Próximo passo: [bold]dockerizer up[/bold] para iniciar os serviços.")


def _start(docker: DockerManager, store: ManifestStore, manifest: Manifest, run: RunRecord) -> None:
    ui.info("[bold]Iniciando serviços...[/bold]")
    result = docker.up()
    if not result.ok:
        run.status = "falha_inicializacao"
        store.save(manifest)
        fail("Falha ao iniciar os serviços. Veja 'dockerizer logs' para detalhes.")
    if manifest.docker is not None:
        manifest.docker.started = True
    store.save(manifest)
    ui.success("Serviços iniciados. Use 'dockerizer status' e 'dockerizer logs' para acompanhar.")


@app.command()
def up(path: PathArg = None, path_opt: PathOpt = None, dry_run: DryRun = False) -> None:
    """Inicia os serviços."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = require_manifest(store)
    docker = docker_for(root, manifest, manifest.project_name, dry_run=dry_run)
    # Se containers do projeto já existem, as portas ocupadas podem ser deles próprios.
    running = docker.cli_available() and bool(docker.project_containers())
    validation = validate(
        root,
        docker=None if dry_run else docker,
        port_checker=None if running else PORT_CHECKER,
    )
    ui.show_validation(validation)
    if not validation.ok:
        fail("Configuração inválida; serviços não iniciados.")
    if dry_run:
        docker.up()
        return
    require_docker(docker)
    run = RunRecord(id=new_run_id(), command="up", mode=manifest.mode)
    manifest.runs.append(run)
    _start(docker, store, manifest, run)
    run.status = "concluido"
    store.save(manifest)


@app.command()
def down(path: PathArg = None, path_opt: PathOpt = None, dry_run: DryRun = False) -> None:
    """Interrompe os serviços sem remover dados persistentes."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = require_manifest(store)
    docker = docker_for(root, manifest, manifest.project_name, dry_run=dry_run)
    if not dry_run:
        require_docker(docker)
    result = docker.down()
    if not result.ok:
        fail("Falha ao interromper os serviços.")
    if not dry_run:
        ui.success("Serviços interrompidos. Volumes e dados persistentes foram preservados.")


@app.command()
def logs(
    path: PathArg = None,
    path_opt: PathOpt = None,
    service: Annotated[
        str | None, typer.Option("--service", "-s", help="Exibe apenas um serviço.")
    ] = None,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Acompanha os logs.")] = False,
    tail: Annotated[int, typer.Option("--tail", help="Linhas finais por serviço.", min=1)] = 200,
) -> None:
    """Exibe os logs dos serviços."""
    root = project_root(path, path_opt)
    manifest = require_manifest(ManifestStore(root))
    docker = docker_for(root, manifest, manifest.project_name)
    require_docker(docker)
    try:
        result = docker.logs(service, follow, tail)
    except KeyboardInterrupt:
        raise typer.Exit(ExitCode.OK) from None
    if not result.ok:
        fail("Não foi possível obter os logs.")


@app.command("validate")
def validate_command(path: PathArg = None, path_opt: PathOpt = None) -> None:
    """Valida a configuração Docker."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = load_manifest(store)
    docker = docker_for(root, manifest, root.name)
    result = validate(root, docker=docker)
    ui.show_validation(result)
    if not result.ok:
        raise typer.Exit(ExitCode.ERROR)


@app.command()
def status(path: PathArg = None, path_opt: PathOpt = None) -> None:
    """Exibe o estado dos serviços."""
    from rich.table import Table

    root = project_root(path, path_opt)
    manifest = require_manifest(ManifestStore(root))
    last = manifest.runs[-1] if manifest.runs else None
    mode = {"production": "produção", "development": "desenvolvimento"}.get(
        manifest.mode or "", "—"
    )
    ui.info(f"[bold]Projeto:[/bold] {manifest.project_name}  [bold]Modo:[/bold] {mode}")
    if last:
        ui.info(
            f"[bold]Última execução:[/bold] {last.command} ({last.status}) em {last.started_at:%Y-%m-%d %H:%M} UTC"
        )
    ui.info(f"[bold]Arquivos gerenciados:[/bold] {len(manifest.files)}")
    docker = docker_for(root, manifest, manifest.project_name)
    require_docker(docker)
    try:
        containers = docker.ps()
    except DockerCommandError as exc:
        fail(str(exc))
    if not containers:
        ui.info("Nenhum container do projeto encontrado. Use 'dockerizer up' para iniciar.")
        return
    table = Table(title="Serviços")
    for column in ("Serviço", "Estado", "Saúde", "Portas"):
        table.add_column(column)
    for item in containers:
        publishers = item.get("Publishers") or []
        # IPv4 e IPv6 aparecem como publicações separadas: exibe cada par uma única vez.
        pairs = dict.fromkeys(
            f"{p.get('PublishedPort')}→{p.get('TargetPort')}"
            for p in publishers
            if isinstance(p, dict) and p.get("PublishedPort")
        )
        ports = ", ".join(pairs)
        table.add_row(
            str(item.get("Service", item.get("Name", "?"))),
            str(item.get("State", "?")),
            str(item.get("Health") or "—"),
            ports or "—",
        )
    ui.console.print(table)


@app.command()
def rollback(
    path: PathArg = None,
    path_opt: PathOpt = None,
    dry_run: DryRun = False,
    yes: Yes = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Reverte também arquivos editados manualmente (a versão atual é salva em backup).",
        ),
    ] = False,
) -> None:
    """Desfaz as alterações realizadas pela ferramenta (volumes são preservados)."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = require_manifest(store)
    plan = plan_rollback(root, store, manifest, force)
    ui.show_rollback_plan(plan)
    if manifest.docker:
        ui.info(
            f"Containers e redes do projeto Compose '{manifest.docker.compose_project}' serão "
            "removidos. [bold]Volumes serão preservados.[/bold]"
        )
    if not dry_run and not yes and not ui.confirm("Prosseguir com a reversão?", False):
        fail("Operação cancelada; nada foi alterado.")
    docker = docker_for(root, manifest, manifest.project_name, dry_run=dry_run)
    result = execute_rollback(root, store, manifest, plan, docker, dry_run=dry_run)
    for message in result.docker_messages:
        ui.info(message)
    if dry_run:
        ui.info("[bold]Dry-run:[/bold] nenhuma alteração realizada.")
        if result.conflicts:
            raise typer.Exit(ExitCode.CONFLICT)
        return
    for rel in result.removed:
        ui.success(f"Removido: {rel}")
    for rel in result.restored:
        ui.success(f"Restaurado: {rel}")
    for rel in result.safety_backups:
        ui.warn(f"Versão modificada salva em: {rel}")
    if result.volumes_preserved:
        ui.warn(
            "Volumes preservados: "
            + ", ".join(result.volumes_preserved)
            + ". Para apagá-los definitivamente: dockerizer purge"
        )
    if result.tool_dir_removed:
        ui.success(f"Reversão concluída; {TOOL_DIR}/ removido.")
    elif not result.conflicts:
        ui.info(f"{TOOL_DIR}/ mantido (volumes, backups ou containers pendentes).")
    if result.conflicts:
        for item in result.conflicts:
            ui.error(f"Conflito: {item.record.path} — {item.reason}")
        fail(
            "Reversão parcial: arquivos em conflito foram mantidos. Revise-os ou use --force.",
            ExitCode.CONFLICT,
        )


@app.command()
def purge(path: PathArg = None, path_opt: PathOpt = None, dry_run: DryRun = False) -> None:
    """[red]Remove definitivamente[/red] os volumes (dados persistentes) do projeto."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = require_manifest(store)
    docker = docker_for(root, manifest, manifest.project_name, dry_run=dry_run)
    require_docker(docker)
    plan = plan_purge(manifest, docker)
    if not plan.volumes:
        ui.info("Nenhum volume gerenciado pelo Dockerizer encontrado para este projeto.")
        return
    if plan.running_containers:
        fail("Há containers do projeto existentes. Execute 'dockerizer down' antes do purge.")
    ui.warn("Os seguintes volumes e TODOS os dados neles serão apagados de forma irreversível:")
    for volume in plan.volumes:
        ui.info(f"  - {volume}")
    if dry_run:
        docker.remove_volumes(plan.volumes)
        ui.info("[bold]Dry-run:[/bold] nenhum volume removido.")
        return
    project = (
        manifest.docker.compose_project
        if manifest.docker
        else compose_project_name(manifest.project_name)
    )
    try:
        typed = typer.prompt(f"Para confirmar, digite o nome do projeto ({project})")
    except typer.Abort:
        fail("Operação cancelada; nenhum volume removido.")
    if typed.strip() != project:
        fail("Confirmação incorreta; nenhum volume removido.")
    try:
        removed = execute_purge(store, manifest, docker, plan)
    except UnsafeDockerOperation as exc:
        fail(str(exc))
    if not removed:
        fail("Falha ao remover os volumes.")
    ui.success("Volumes removidos.")


def main() -> None:
    app()
