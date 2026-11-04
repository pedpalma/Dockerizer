"""Geração de .env.example e .env.

Regras de segurança:
- ``.env.example`` nunca contém segredos (valores sensíveis ficam vazios).
- ``.env`` só é criado quando não existe; se existir, apenas variáveis de infraestrutura
  ausentes são acrescentadas. Segredos existentes nunca são regenerados — trocar a senha de um
  banco cujo volume foi preservado impediria o acesso aos dados.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from dockerizer.analyzers.env import find_urls, is_host_machine, parse_env_file
from dockerizer.analyzers.infrastructure import SPECS, env_kind_hint
from dockerizer.constants import HOST_ENV_FILE
from dockerizer.generators.compose import HOST_GATEWAY, INFRA_VARS
from dockerizer.generators.templating import render
from dockerizer.models.architecture import EnvVar, ProjectAnalysis, ServiceKind

GENERATABLE_SECRET_RE = re.compile(r"(^|_)(SECRET_KEY|SECRET|SIGNING_KEY|JWT_SECRET|APP_KEY)$")
APPEND_HEADER = "# --- Adicionado pelo Dockerizer (infraestrutura local) ---"


def generate_secret() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class EnvGroup:
    title: str
    lines: list[str] = field(default_factory=list)


def _db_name(project: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", project) or "app"


def infra_defaults(
    analysis: ProjectAnalysis,
    known: dict[str, str],
    secret: Callable[[], str] = generate_secret,
) -> dict[str, str]:
    """Valores das variáveis de infraestrutura: reaproveita os conhecidos, gera os ausentes."""
    values: dict[str, str] = {}
    for service in analysis.local_services():
        for var in INFRA_VARS[service.kind]:
            if var.name in known:
                values[var.name] = known[var.name]
            elif var.default is None:
                values[var.name] = secret()
            else:
                values[var.name] = var.default.format(db=_db_name(analysis.project_name))
    return values


def _infra_names(analysis: ProjectAnalysis) -> set[str]:
    return {v.name for s in analysis.local_services() for v in INFRA_VARS[s.kind]}


def _wired(overrides: dict[str, dict[str, str]]) -> dict[str, str]:
    """Variável → aplicação, para variáveis definidas diretamente pelo compose.yaml."""
    wired: dict[str, str] = {}
    for app, env in overrides.items():
        for name in env:
            wired.setdefault(name, app)
    return wired


def _app_groups(
    analysis: ProjectAnalysis,
    overrides: dict[str, dict[str, str]],
    line: Callable[[EnvVar], str],
) -> list[EnvGroup]:
    infra = _infra_names(analysis)
    wired = _wired(overrides)
    app_group = EnvGroup("Aplicação")
    build_group = EnvGroup("Build do frontend (valores públicos, embutidos no bundle)")
    build_vars = {v for a in analysis.applications for v in a.build_args}
    for var in analysis.env_vars:
        if var.name in infra:
            continue
        if var.name in wired:
            app_group.lines.append(
                f"# {var.name}: definida pelo compose.yaml (aplicação '{wired[var.name]}')"
            )
            continue
        target = build_group if var.name in build_vars else app_group
        target.lines.append(line(var))
    return [g for g in (app_group, build_group) if g.lines]


def _infra_group_lines(analysis: ProjectAnalysis, values: dict[str, str] | None) -> list[EnvGroup]:
    groups = []
    for service in analysis.local_services():
        variables = INFRA_VARS[service.kind]
        if not variables:
            continue
        group = EnvGroup(f"{SPECS[service.kind].label} (serviço local '{service.kind.value}')")
        for var in variables:
            if values is None:
                example = (
                    ""
                    if var.default is None
                    else var.default.format(db=_db_name(analysis.project_name))
                )
                group.lines.append(f"{var.name}={example}")
            else:
                group.lines.append(f"{var.name}={values[var.name]}")
        groups.append(group)
    return groups


def render_env_example(analysis: ProjectAnalysis, overrides: dict[str, dict[str, str]]) -> str:
    def line(var: EnvVar) -> str:
        if var.sensitive or var.example_value is None:
            return f"{var.name}="
        return f"{var.name}={var.example_value}"

    groups = _infra_group_lines(analysis, None) + _app_groups(analysis, overrides, line)
    return render("env.example.j2", groups=groups)


def render_new_env(
    analysis: ProjectAnalysis,
    overrides: dict[str, dict[str, str]],
    infra_values: dict[str, str],
    secret: Callable[[], str] = generate_secret,
) -> str:
    def line(var: EnvVar) -> str:
        if GENERATABLE_SECRET_RE.search(var.name):
            return f"# {var.name}: valor gerado automaticamente\n{var.name}={secret()}"
        if var.example_value is not None and not var.sensitive:
            return f"{var.name}={var.example_value}"
        return f"# {var.name}=  (preencha se a aplicação precisar)"

    groups = _infra_group_lines(analysis, infra_values) + _app_groups(analysis, overrides, line)
    return render("env.j2", groups=groups)


def append_to_env(existing: str, analysis: ProjectAnalysis, infra_values: dict[str, str]) -> str:
    """Acrescenta ao .env existente apenas variáveis de infraestrutura ausentes."""
    present = parse_env_file(existing)
    missing = [name for name in infra_values if name not in present]
    if not missing:
        return existing
    block = [APPEND_HEADER, *(f"{name}={infra_values[name]}" for name in missing)]
    separator = "" if existing.endswith("\n") or not existing else "\n"
    prefix = existing + separator + ("\n" if existing else "")
    return prefix + "\n".join(block) + "\n"


HOST_URL_RE = re.compile(
    # userinfo guloso até o último "@" (senhas com "@" sem codificação ainda funcionam)
    r"(?P<prefix>://(?:[^/\s]*@)?)(?:localhost|127\.0\.0\.1|\[::1\])(?=[:/?#]|$)",
    re.IGNORECASE,
)
SAFE_ENV_VALUE_RE = re.compile(r"[A-Za-z0-9_./:@%+=,?&-]*")


def _host_value(name: str, value: str, kinds: set[ServiceKind]) -> str:
    """O mesmo valor, apontando os serviços do host para ``host.docker.internal``."""
    urls = [u for u in find_urls(value) if u.service and ServiceKind(u.service) in kinds]
    if any(is_host_machine(u.host) for u in urls):
        return HOST_URL_RE.sub(rf"\g<prefix>{HOST_GATEWAY}", value)
    if name.endswith("_HOST") and is_host_machine(value) and env_kind_hint(name, kinds):
        return HOST_GATEWAY
    return value


def _env_line(name: str, value: str) -> str | None:
    if SAFE_ENV_VALUE_RE.fullmatch(value):
        return f"{name}={value}"
    if "'" not in value:
        return f"{name}='{value}'"  # aspas simples: o Compose não interpola o valor
    return None


def render_host_env(
    analysis: ProjectAnalysis, env_texts: dict[str, str]
) -> tuple[str | None, list[str]]:
    """Conteúdo de ``.env.docker`` para serviços que rodam no host (``None`` se não há).

    ``env_texts`` são os arquivos .env reais que o Compose carrega, em ordem de precedência.
    O ``.env`` original não é alterado: os processos que rodam fora do Docker continuam
    usando ``localhost``.
    """
    kinds = {s.kind for s in analysis.host_services()}
    if not kinds:
        return None, []
    warnings: list[str] = []
    rewritten: dict[str, str] = {}
    for text in env_texts.values():
        for name, value in parse_env_file(text).items():
            new = _host_value(name, value, kinds)
            if new != value:
                rewritten[name] = new
    lines = []
    for name, value in rewritten.items():
        line = _env_line(name, value)
        if line is None:
            warnings.append(
                f"{name} contém aspas simples e não foi copiada para {HOST_ENV_FILE}; "
                f"defina-a manualmente com o host {HOST_GATEWAY}."
            )
            continue
        lines.append(line)
    if not lines:
        labels = ", ".join(SPECS[k].label for k in sorted(kinds))
        warnings.append(
            f"Nenhuma variável com localhost para {labels} foi encontrada no .env; configure o "
            f"host {HOST_GATEWAY} manualmente."
        )
        return None, warnings
    header = [
        "# Gerado pelo Dockerizer a partir do .env: as mesmas variáveis com localhost trocado por",
        f"# {HOST_GATEWAY}, para que os containers alcancem os serviços desta máquina.",
        "# Carregado pelo compose.yaml depois do .env. Rode 'dockerizer setup' de novo se o .env",
        "# mudar. Contém segredos: NÃO versione este arquivo.",
    ]
    return "\n".join([*header, *lines]) + "\n", warnings
