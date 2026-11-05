"""Interface de linha de comando do Dockerizer."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from dockerizer import __version__
from dockerizer.analyzers.project import analyze_project
from dockerizer.backup.dumps import (
    BackupError,
    create_backup,
    data_services,
    find_backup,
    restore_backup,
    running_apps,
    verify_backup,
)
from dockerizer.cli import i18n, ui
from dockerizer.constants import COMPOSE_FILE, HOST_ENV_FILE, TOOL_DIR, compose_project_name
from dockerizer.docker.manager import (
    DockerCommandError,
    DockerManager,
    Runner,
    UnsafeDockerOperation,
    compose_version_error,
)
from dockerizer.fs.changes import ChangeAction, apply_changes, plan_changes
from dockerizer.generators.dockerfile import GenerationError
from dockerizer.generators.plan import (
    assign_ports,
    build_plan,
    choose_publish_ip,
    port_is_free,
    previous_host_ports,
    stale_host_env,
)
from dockerizer.models.architecture import Mode
from dockerizer.reporting.report import write_reports
from dockerizer.rollback.manager import (
    execute_purge,
    execute_rollback,
    plan_purge,
    plan_rollback,
)
from dockerizer.rollback.manifest import (
    BackupReason,
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


def _remove_images(docker: DockerManager, names: list[str], containers_removed: bool) -> None:
    if not containers_removed:
        ui.warn("Imagens não removidas: os containers do projeto não foram removidos.")
        return
    for name in sorted(set(names)):
        result = docker.remove_image(name)
        if result.dry_run:
            continue
        if result.ok:
            ui.success(f"Imagem removida: {name}")
        elif "No such image" in result.stderr or "No such image" in result.stdout:
            ui.info(f"[dim]Imagem {name} já não existia.[/dim]")
        else:
            ui.warn(f"Imagem {name} não removida: {result.stderr.strip()}")


def warn_stale_host_env(root: Path, manifest: Manifest) -> None:
    stale = stale_host_env(root, manifest.host_env_sources)
    if stale:
        ui.warn(
            f"{', '.join(stale)} mudou depois da geração de {HOST_ENV_FILE}: os containers ainda "
            "usam os valores antigos. Rode 'dockerizer setup' para atualizá-lo."
        )


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


def require_docker(docker: DockerManager, uses_compose_file: bool = True) -> None:
    """Docker disponível e, se o comando lê o compose.yaml, Compose em versão compatível.

    ``purge`` não lê o compose.yaml (trabalha por label) e continua funcionando com um
    Compose antigo, para que os dados nunca fiquem presos a uma atualização.
    """
    if not docker.cli_available():
        fail("Docker CLI com o plugin Compose não encontrado. Instale o Docker e tente novamente.")
    if uses_compose_file and (error := compose_version_error(docker.compose_version())):
        fail(error)
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
    diff: Annotated[
        bool,
        typer.Option(
            "--diff",
            help="Mostra o conteúdo que seria gravado em cada arquivo (diff; segredos ocultos).",
        ),
    ] = False,
    host_ip: Annotated[
        str | None,
        typer.Option(
            "--host-ip",
            help="Endereço do host em que as portas são publicadas (ex.: 127.0.0.1 ou 0.0.0.0). "
            "Padrão: 127.0.0.1 se o projeto tem nginx, senão todas as interfaces.",
        ),
    ] = None,
) -> None:
    """Analisa o projeto, gera os arquivos Docker e valida a configuração."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = load_manifest(store)
    mode = Mode.DEVELOPMENT if dev else Mode.PRODUCTION
    if host_ip is not None:
        try:
            ipaddress.ip_address(host_ip)
        except ValueError:
            fail(f"--host-ip inválido: {host_ip}")

    analysis = analyze_project(root, generated_paths(manifest), mode)
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
    publish_ip, publish_reason = choose_publish_ip(
        analysis, host_ip, manifest.publish_ip if manifest else None
    )
    if publish_reason:
        ui.info(f"[dim]{publish_reason}[/dim]")
    try:
        plan = build_plan(
            analysis, root, mode, host_ports, store.load_credentials(), publish_ip=publish_ip
        )
    except GenerationError as exc:
        fail(str(exc))
    changes = plan_changes(root, plan.files, manifest, force)
    ui.show_changes(changes)
    if diff:
        ui.show_diffs(root, changes)
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
    manifest.host_env_sources = plan.host_env_sources
    if host_ip is not None:
        manifest.publish_ip = host_ip
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
    warn_stale_host_env(root, manifest)
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
    warn_stale_host_env(root, manifest)
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
    images: Annotated[
        bool,
        typer.Option(
            "--images",
            help="Remove também as imagens construídas pelo Dockerizer (não afeta imagens base).",
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
    if images and manifest.docker and manifest.docker.images:
        _remove_images(docker, manifest.docker.images, result.docker_cleaned or dry_run)
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
    if manifest.data_backups:
        ui.warn(
            f"Backups de dados preservados em {TOOL_DIR}/data-backups/: o Dockerizer nunca os "
            "apaga. Remova-os manualmente quando não precisar mais deles."
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
    require_docker(docker, uses_compose_file=False)
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
    if _offer_backup_before_purge(root, store, manifest, docker):
        plan = plan_purge(manifest, docker)  # o backup sobe e derruba a infraestrutura
        if plan.running_containers:
            fail("Há containers do projeto existentes. Execute 'dockerizer down' antes do purge.")
    project = _compose_project(manifest)
    typed = _typed_confirmation(project, "nenhum volume removido")
    if typed != project:
        fail("Confirmação incorreta; nenhum volume removido.")
    try:
        removed = execute_purge(store, manifest, docker, plan)
    except UnsafeDockerOperation as exc:
        fail(str(exc))
    if not removed:
        fail("Falha ao remover os volumes.")
    ui.success("Volumes removidos.")


def _compose_project(manifest: Manifest) -> str:
    if manifest.docker:
        return manifest.docker.compose_project
    return compose_project_name(manifest.project_name)


def _typed_confirmation(project: str, cancelled: str) -> str:
    try:
        return str(typer.prompt(f"Para confirmar, digite o nome do projeto ({project})")).strip()
    except typer.Abort:
        fail(f"Operação cancelada; {cancelled}.")


def _run_backup(
    store: ManifestStore,
    manifest: Manifest,
    docker: DockerManager,
    services: list[str],
    reason: BackupReason,
) -> str | None:
    """Gera o backup e o exibe; devolve o id (``None`` em dry-run)."""
    try:
        backup = create_backup(store, manifest, docker, services, reason)
    except BackupError as exc:
        fail(str(exc))
    if backup is None:
        return None
    location = (store.data_backups_dir / backup.id).relative_to(store.root).as_posix() + "/"
    ui.show_backup(backup, location)
    return backup.id


def _offer_backup_before_purge(
    root: Path, store: ManifestStore, manifest: Manifest, docker: DockerManager
) -> bool:
    """Pergunta (padrão: sim) se deve gerar um backup antes do purge. True se gerou."""
    services, warnings = data_services(manifest)
    for warning in warnings:
        ui.warn(warning)
    if not services:
        return False
    if not (root / (manifest.docker.compose_file if manifest.docker else COMPOSE_FILE)).is_file():
        ui.warn(
            f"{COMPOSE_FILE} não existe mais: não é possível subir os serviços para um backup. "
            "Para fazê-lo, rode 'dockerizer setup' e 'dockerizer backup' antes do purge."
        )
        return False
    if not ui.confirm("Gerar um backup dos dados antes de apagá-los?", True):
        return False
    require_docker(docker)
    ui.info("Os serviços de dados serão iniciados só para o backup e parados em seguida.")
    _run_backup(store, manifest, docker, services, "antes-do-purge")
    return True


@app.command()
def backup(path: PathArg = None, path_opt: PathOpt = None, dry_run: DryRun = False) -> None:
    """Gera um backup (dump) dos bancos e do Redis em .dockerizer/data-backups/."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = require_manifest(store)
    services, warnings = data_services(manifest)
    for warning in warnings:
        ui.warn(warning)
    if not services:
        ui.info("Nenhum serviço de dados local com backup suportado neste projeto.")
        return
    docker = docker_for(root, manifest, manifest.project_name, dry_run=dry_run)
    require_docker(docker)
    _run_backup(store, manifest, docker, services, "manual")
    if dry_run:
        ui.info("[bold]Dry-run:[/bold] nenhum backup gerado.")
        return
    ui.info(
        "O Dockerizer nunca apaga backups (nem no rollback ou purge). "
        "Para restaurar: dockerizer restore"
    )


@app.command()
def restore(
    path: PathArg = None,
    path_opt: PathOpt = None,
    dry_run: DryRun = False,
    backup_id: Annotated[
        str | None,
        typer.Option("--backup", help="ID do backup (padrão: o mais recente).", show_default=False),
    ] = None,
    list_only: Annotated[
        bool, typer.Option("--list", help="Lista os backups disponíveis e sai.")
    ] = False,
) -> None:
    """[red]Substitui[/red] os dados atuais pelos de um backup (com backup de segurança antes)."""
    root = project_root(path, path_opt)
    store = ManifestStore(root)
    manifest = require_manifest(store)
    if list_only or not manifest.data_backups:
        if not manifest.data_backups:
            fail("Nenhum backup registrado. Gere um com: dockerizer backup")
        ui.show_backups(manifest.data_backups)
        return
    chosen = find_backup(manifest, backup_id)
    if chosen is None:
        fail(
            f"Backup '{backup_id}' não encontrado. Veja os disponíveis com: dockerizer restore --list"
        )
    problems = verify_backup(store, chosen)
    if problems:
        fail("Backup corrompido; nada foi alterado. " + " ".join(problems))
    services, _ = data_services(manifest)
    restorable = [d.service for d in chosen.dumps if d.service in services]
    for dump in chosen.dumps:
        if dump.service not in services:
            ui.warn(f"'{dump.service}' não existe mais no projeto; o dump dele será ignorado.")
    if not restorable:
        fail("Nenhum serviço do backup existe no projeto atual.")
    docker = docker_for(root, manifest, manifest.project_name, dry_run=dry_run)
    require_docker(docker)
    try:
        apps = running_apps(docker, services)
    except BackupError as exc:
        fail(str(exc))
    if apps:
        fail(
            "Aplicações em execução (" + ", ".join(apps) + ") usam os dados. "
            "Execute 'dockerizer down' antes do restore."
        )
    ui.show_backups([chosen])
    ui.warn(
        "Os dados atuais de " + ", ".join(restorable) + " serão substituídos pelos do backup "
        f"{chosen.id}. Um backup de segurança do estado atual é gerado antes."
    )
    if dry_run:
        try:
            restore_backup(store, docker, chosen, restorable)
        except BackupError as exc:
            fail(str(exc))
        ui.info("[bold]Dry-run:[/bold] nenhum dado alterado.")
        return
    project = _compose_project(manifest)
    if _typed_confirmation(project, "nada foi alterado") != project:
        fail("Confirmação incorreta; nada foi alterado.")
    safety = _run_backup(store, manifest, docker, restorable, "antes-do-restore")
    try:
        restored = restore_backup(store, docker, chosen, restorable)
    except BackupError as exc:
        fail(f"{exc} O estado anterior está no backup {safety}.")
    ui.success(
        f"Dados restaurados ({', '.join(restored)}) a partir de {chosen.id}. "
        f"Estado anterior salvo no backup {safety}."
    )


def main() -> None:
    app()
