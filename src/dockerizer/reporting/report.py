"""Relatórios da análise (Markdown legível e JSON estruturado). Nunca incluem segredos:
apenas nomes de variáveis e valores de exemplo não sensíveis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from dockerizer.fs.changes import PlannedChange
from dockerizer.fs.paths import atomic_write
from dockerizer.models.architecture import ProjectAnalysis
from dockerizer.rollback.manifest import ManifestStore, now
from dockerizer.validators.validation import ValidationResult


@dataclass
class ReportPaths:
    markdown: Path
    json: Path


def render_markdown(
    analysis: ProjectAnalysis,
    changes: list[PlannedChange] | None = None,
    validation: ValidationResult | None = None,
) -> str:
    out: list[str] = [f"# Relatório do Dockerizer — {analysis.project_name}", ""]
    out.append(f"Gerado em {now().strftime('%Y-%m-%d %H:%M:%S UTC')}.")
    out.append("")
    out.append("## Aplicações")
    if not analysis.applications:
        out.append("Nenhuma aplicação suportada encontrada.")
    for app in analysis.applications:
        frameworks = ", ".join(f.value for f in app.frameworks) or "—"
        out.append(f"### {app.name} (`{app.path}`)")
        out.append(f"- Linguagem: {app.language.value} {app.language_version}")
        out.append(f"- Frameworks: {frameworks}")
        out.append(f"- Gerenciador de dependências: {app.package_manager.value}")
        out.append(f"- Papel: {app.role.value}")
        for process in app.processes:
            command = process.command.display() if process.command else "**pendente**"
            port = f", porta {process.port}" if process.port else ""
            out.append(f"- Processo `{process.name}` ({process.kind.value}{port}): `{command}`")
        if app.migration:
            out.append(f"- Migrações ({app.migration.tool}): `{' '.join(app.migration.command)}`")
        if app.existing_dockerfile:
            out.append(f"- Dockerfile existente preservado: `{app.existing_dockerfile}`")
        for data_dir in app.data_dirs:
            out.append(
                f"- Dados persistentes: `{data_dir.path}` → volume `{data_dir.volume}` "
                f"({data_dir.source})"
            )
        for item in app.evidence:
            out.append(f"  - evidência: {item}")
        for warning in app.warnings:
            out.append(f"  - aviso: {warning}")
        out.append("")
    out.append("## Serviços de infraestrutura")
    if not analysis.services:
        out.append("Nenhum serviço de infraestrutura identificado.")
    for service in analysis.services:
        out.append(f"### {service.kind.value} — {service.decision.value}")
        out.append(f"- {service.reason}")
        if service.used_by:
            out.append(f"- Usado por: {', '.join(service.used_by)}")
        for evidence in service.evidence:
            out.append(
                f"  - [{evidence.strength.value}] {evidence.description} (`{evidence.source}`)"
            )
        out.append("")
    if analysis.reverse_proxies:
        out.append("## Proxy reverso existente (nginx) — não reproduzido")
        for proxy in analysis.reverse_proxies:
            names = ", ".join(proxy.server_names) or "sem server_name"
            tls = ", TLS" if proxy.tls else ""
            out.append(f"### `{proxy.path}` ({names}{tls})")
            for route in proxy.routes:
                target_app = f" → aplicação `{route.app}`" if route.app else ""
                out.append(f"- `location {route.location}`: `{route.target}`{target_app}")
            for setting in proxy.settings:
                out.append(f"- ajuste: `{setting}`")
            out.append("")
    out.append("## Variáveis de ambiente")
    for var in analysis.env_vars:
        flag = " (sensível)" if var.sensitive else ""
        out.append(f"- `{var.name}`{flag} — {', '.join(var.sources)}")
    if not analysis.env_vars:
        out.append("Nenhuma variável identificada.")
    out.append("")
    if analysis.existing_docker_files:
        out.append("## Arquivos Docker existentes")
        for docker_file in analysis.existing_docker_files:
            origin = "gerado pelo Dockerizer" if docker_file.managed else "preexistente"
            out.append(f"- `{docker_file.path}` ({docker_file.kind}, {origin})")
        out.append("")
    if changes:
        out.append("## Alterações planejadas")
        for change in changes:
            out.append(f"- `{change.file.path}`: {change.action.value} — {change.reason}")
        out.append("")
    if validation is not None:
        out.append("## Validação")
        if not validation.issues:
            out.append("Nenhum problema encontrado.")
        for issue in validation.issues:
            where = f" (`{issue.file}`)" if issue.file else ""
            out.append(f"- **{issue.level.value}**: {issue.message}{where}")
        out.append("")
    if analysis.warnings or analysis.notes:
        out.append("## Avisos e observações")
        out.extend(f"- {w}" for w in analysis.warnings + analysis.notes)
        out.append("")
    if analysis.questions:
        out.append("## Pendências (exigem confirmação)")
        out.extend(f"- {q.prompt}" for q in analysis.questions)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_reports(
    store: ManifestStore,
    analysis: ProjectAnalysis,
    changes: list[PlannedChange] | None = None,
    validation: ValidationResult | None = None,
) -> ReportPaths:
    store.ensure_dirs()
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    markdown = store.reports_dir / f"analise-{stamp}.md"
    data = analysis.model_dump(mode="json")
    if validation is not None:
        data["validation"] = [
            {"level": i.level.value, "message": i.message, "file": i.file}
            for i in validation.issues
        ]
    json_path = store.reports_dir / f"analise-{stamp}.json"
    atomic_write(markdown, render_markdown(analysis, changes, validation).encode(), mode=0o600)
    atomic_write(
        json_path,
        json.dumps(data, indent=2, ensure_ascii=False).encode() + b"\n",
        mode=0o600,
    )
    return ReportPaths(markdown=markdown, json=json_path)
