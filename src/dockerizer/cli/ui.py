"""Apresentação (Rich) e interação com o usuário, em português."""

from __future__ import annotations

import shlex
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from dockerizer.analyzers.resolution import AnswerError, apply_answer
from dockerizer.fs.changes import ChangeAction, PlannedChange
from dockerizer.generators.plan import PortAssignment
from dockerizer.models.architecture import ProjectAnalysis, Question, QuestionKind
from dockerizer.rollback.manager import ItemStatus, RollbackPlan
from dockerizer.rollback.manifest import DataBackup
from dockerizer.validators.validation import Level, ValidationResult

console = Console(soft_wrap=True, highlight=False)

YES_WORDS = {"s", "sim", "y", "yes"}
NO_WORDS = {"n", "nao", "não", "no"}
ACTION_STYLE = {
    ChangeAction.CREATE: "green",
    ChangeAction.UPDATE: "cyan",
    ChangeAction.REPLACE: "yellow",
    ChangeAction.APPEND: "cyan",
    ChangeAction.UNCHANGED: "dim",
    ChangeAction.SKIP: "dim",
    ChangeAction.CONFLICT: "bold red",
}


def info(message: str) -> None:
    console.print(message)


# As mensagens trazem texto do projeto analisado (caminhos, "[program:x]", "[Service]"...):
# escapá-las impede que o Rich as interprete como marcação e omita trechos.
def success(message: str) -> None:
    console.print(f"[green]✔[/green] {escape(message)}")


def warn(message: str) -> None:
    console.print(f"[yellow]![/yellow] {escape(message)}")


def error(message: str) -> None:
    console.print(f"[bold red]✖[/bold red] {escape(message)}")


def show_command(args: list[str], simulated: bool) -> None:
    prefix = "[dim](dry-run) executaria:[/dim]" if simulated else "[dim]$[/dim]"
    console.print(f"{prefix} {shlex.join(args)}")


def confirm(question: str, default: bool = False) -> bool:
    suffix = "[S/n]" if default else "[s/N]"
    while True:
        try:
            answer = typer.prompt(f"{question} {suffix}", default="", show_default=False)
        except typer.Abort:
            return False
        answer = answer.strip().lower()
        if not answer:
            return default
        if answer in YES_WORDS:
            return True
        if answer in NO_WORDS:
            return False
        warn("Responda 's' (sim) ou 'n' (não).")


def show_analysis(analysis: ProjectAnalysis) -> None:
    console.print(
        Panel.fit(f"[bold]Análise do projeto[/bold] {analysis.project_name}\n{analysis.root}")
    )
    if analysis.applications:
        table = Table(title="Aplicações", show_lines=True)
        for column in ("Nome", "Caminho", "Linguagem", "Frameworks", "Gerenciador", "Processos"):
            table.add_column(column)
        for app in analysis.applications:
            processes = []
            for process in app.processes:
                command = (
                    escape(process.command.display()) if process.command else "[red]pendente[/red]"
                )
                port = f" :{process.port}" if process.port else ""
                processes.append(f"[bold]{escape(process.name)}[/bold]{port} → {command}")
            if app.migration:
                migration = escape(" ".join(app.migration.command))
                processes.append(f"[bold]migrações[/bold] → {migration}")
            for data_dir in app.data_dirs:
                processes.append(
                    f"[bold]dados[/bold] {escape(data_dir.path)} → volume {data_dir.volume}"
                )
            table.add_row(
                app.name,
                app.path,
                f"{app.language.value} {app.language_version}",
                ", ".join(f.value for f in app.frameworks) or "—",
                app.package_manager.value,
                "\n".join(processes) or "—",
            )
        console.print(table)
    if analysis.services:
        table = Table(title="Serviços de infraestrutura")
        for column in ("Serviço", "Decisão", "Motivo", "Usado por"):
            table.add_column(column)
        for service in analysis.services:
            table.add_row(
                service.kind.value,
                service.decision.value,
                service.reason,
                ", ".join(service.used_by) or "—",
            )
        console.print(table)
    if analysis.reverse_proxies:
        table = Table(title="Proxy reverso existente (nginx) — não reproduzido", show_lines=True)
        for column in ("Arquivo", "Servidor", "Rota", "Destino", "Aplicação"):
            table.add_column(column)
        for proxy in analysis.reverse_proxies:
            server = escape(", ".join(proxy.server_names) or "—")
            if proxy.tls:
                server += " [yellow](TLS)[/yellow]"
            if proxy.settings:
                server += "\n[dim]" + escape("\n".join(proxy.settings)) + "[/dim]"
            routes = proxy.routes or []
            if not routes:
                table.add_row(proxy.path, server, "—", ", ".join(proxy.listens), "—")
            for index, route in enumerate(routes):
                table.add_row(
                    escape(proxy.path) if index == 0 else "",
                    server if index == 0 else "",
                    escape(route.location),
                    escape(route.target),
                    route.app or "—",
                )
        console.print(table)
    if analysis.env_vars:
        names = [f"{v.name}{' (sensível)' if v.sensitive else ''}" for v in analysis.env_vars]
        console.print(f"[bold]Variáveis de ambiente:[/bold] {', '.join(names)}")
    if analysis.existing_docker_files:
        files = [
            f"{f.path}{' (gerado pelo Dockerizer)' if f.managed else ''}"
            for f in analysis.existing_docker_files
        ]
        console.print(f"[bold]Arquivos Docker existentes:[/bold] {', '.join(files)}")
    for app in analysis.applications:
        for warning in app.warnings:
            warn(f"[{app.name}] {warning}")
    for warning in analysis.warnings:
        warn(warning)
    for note in analysis.notes:
        console.print(f"[dim]• {escape(note)}[/dim]")
    if analysis.questions:
        warn(f"{len(analysis.questions)} item(ns) precisam de confirmação antes da geração.")


def resolve_questions(
    analysis: ProjectAnalysis, assume_yes: bool, answers: dict[str, str] | None = None
) -> list[Question]:
    """Pergunta ao usuário o que não foi identificado. Retorna perguntas não resolvidas.

    ``answers`` traz respostas dadas em execuções anteriores (reaplicadas sem perguntar) e
    recebe as novas respostas do usuário, para que um ``setup --yes`` posterior as reutilize.
    """
    unresolved: list[Question] = []
    answers = answers if answers is not None else {}
    for question in list(analysis.questions):
        previous = answers.get(question.id)
        if previous is not None:
            try:
                apply_answer(analysis, question, previous)
                reused = escape(f"{question.prompt} → {previous}")
                info(f"[dim]Resposta registrada reutilizada: {reused}[/dim]")
                continue
            except AnswerError:
                answers.pop(question.id, None)
        console.print(
            Panel(
                f"[bold]{escape(question.prompt)}[/bold]\n[dim]{escape(question.help)}[/dim]"
                if question.help
                else f"[bold]{escape(question.prompt)}[/bold]",
                title="Confirmação necessária",
                border_style="yellow",
            )
        )
        if question.kind is QuestionKind.COMMAND and question.choices:
            for index, choice in enumerate(question.choices, start=1):
                console.print(f"  {index}) {choice}")
            console.print("  [dim]Digite o número de uma opção ou um comando completo.[/dim]")
        elif question.choices:
            console.print(f"  Opções: {', '.join(question.choices)}")
        if assume_yes:
            if question.default is None:
                unresolved.append(question)
                warn("Sem valor padrão seguro; não é possível responder automaticamente (--yes).")
                continue
            apply_answer(analysis, question, question.default)
            warn(f"Usando '{question.default}' automaticamente (--yes).")
            continue
        while True:
            try:
                answer = typer.prompt(
                    "Resposta",
                    default=question.default or "",
                    show_default=bool(question.default),
                )
            except typer.Abort:
                unresolved.append(question)
                break
            answer = answer.strip()
            if question.kind is QuestionKind.COMMAND and answer.isdigit():
                index = int(answer) - 1
                if 0 <= index < len(question.choices):
                    answer = question.choices[index]
            try:
                apply_answer(analysis, question, answer)
                answers[question.id] = answer
                break
            except AnswerError as exc:
                error(str(exc))
    analysis.questions = unresolved
    return unresolved


def resolve_ports(assignments: list[PortAssignment], assume_yes: bool) -> dict[str, int] | None:
    ports: dict[str, int] = {}
    for item in assignments:
        if item.conflict:
            warn(
                f"Porta {item.preferred} do host está em uso; '{item.service}' pode usar a porta "
                f"{item.host_port}."
            )
            if not assume_yes and not confirm(
                f"Publicar '{item.service}' na porta {item.host_port}?", True
            ):
                error("Libere a porta ou edite o compose.yaml manualmente após a geração.")
                return None
        ports[item.service] = item.host_port
    return ports


def show_changes(changes: list[PlannedChange]) -> None:
    table = Table(title="Arquivos")
    for column in ("Arquivo", "Ação", "Detalhe"):
        table.add_column(column)
    for change in changes:
        style = ACTION_STYLE[change.action]
        table.add_row(change.file.path, f"[{style}]{change.action.value}[/{style}]", change.reason)
    console.print(table)


DIFF_STYLES = (("+++", "bold"), ("---", "bold"), ("@@", "cyan"), ("+", "green"), ("-", "red"))


def show_diffs(root: Path, changes: list[PlannedChange]) -> None:
    """Conteúdo exato que cada alteração grava (valores sensíveis ocultos).

    Linhas longas são quebradas, nunca cortadas: o objetivo é revisar o conteúdo inteiro.
    """
    from rich.text import Text

    from dockerizer.reporting.diff import change_diff

    shown = False
    for change in changes:
        diff = change_diff(root, change)
        if diff is None:
            continue
        shown = True
        title = escape(change.file.path)
        if change.action is ChangeAction.CONFLICT:
            title += " [bold red](conflito: não será gravado)[/bold red]"
        console.rule(title, align="left")
        for line in diff.splitlines():
            style = next((st for prefix, st in DIFF_STYLES if line.startswith(prefix)), "")
            console.print(Text(line, style=style), soft_wrap=False, overflow="fold")
    if shown:
        console.rule()
        info(
            "[dim]Valores sensíveis (senhas, tokens, URLs com credenciais) aparecem como ********.[/dim]"
        )
    else:
        info("[dim]Nenhum arquivo seria alterado.[/dim]")


def show_validation(result: ValidationResult) -> None:
    if not result.issues:
        detail = " (incluindo docker compose config)" if result.docker_checked else ""
        success(f"Validação concluída sem problemas{detail}.")
        return
    for issue in result.issues:
        where = f" ({issue.file})" if issue.file else ""  # error/warn escapam o markup
        if issue.level is Level.ERROR:
            error(f"{issue.message}{where}")
        else:
            warn(f"{issue.message}{where}")
    if result.ok:
        success("Validação concluída (somente avisos).")


def show_rollback_plan(plan: RollbackPlan) -> None:
    table = Table(title="Reversão de arquivos")
    for column in ("Arquivo", "Ação", "Detalhe"):
        table.add_column(column)
    styles = {
        ItemStatus.REMOVE: "yellow",
        ItemStatus.RESTORE: "cyan",
        ItemStatus.GONE: "dim",
        ItemStatus.CONFLICT: "bold red",
    }
    for item in plan.items:
        style = styles[item.status]
        table.add_row(item.record.path, f"[{style}]{item.status.value}[/{style}]", item.reason)
    console.print(table)


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def show_backup(backup: DataBackup, location: str) -> None:
    table = Table(title=f"Backup {backup.id}")
    for column in ("Serviço", "Arquivo", "Tamanho"):
        table.add_column(column)
    for dump in backup.dumps:
        table.add_row(dump.service, escape(dump.file), _size(dump.size))
    console.print(table)
    success(f"Backup salvo em {location} (permissão 600; não versione).")


def show_backups(backups: list[DataBackup]) -> None:
    table = Table(title="Backups de dados")
    for column in ("ID", "Criado em (UTC)", "Motivo", "Serviços", "Tamanho"):
        table.add_column(column)
    for backup in backups:
        table.add_row(
            backup.id,
            f"{backup.created_at:%Y-%m-%d %H:%M}",
            backup.reason,
            ", ".join(d.service for d in backup.dumps),
            _size(sum(d.size for d in backup.dumps)),
        )
    console.print(table)
