"""Decisão sobre serviços de infraestrutura (PostgreSQL, MySQL, Redis, RabbitMQ).

Uma dependência (driver/cliente) é apenas evidência **fraca**: indica que o projeto *pode*
usar o serviço, mas não se ele é local ou externo. A decisão segue níveis:

1. Evidências **fortes** (``.env`` real, Compose existente) decidem.
2. Na ausência delas, evidências **médias** (exemplos de ``.env``, URLs/configurações no código).
3. Sem nada conclusivo, o serviço fica **indeterminado** e o usuário é consultado.

Evidências conflitantes no mesmo nível também resultam em consulta ao usuário.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from dockerizer.analyzers.env import find_urls, is_host_machine, parse_env_file
from dockerizer.models.architecture import (
    Application,
    Evidence,
    EvidenceStrength,
    Framework,
    InfraService,
    Language,
    ServiceDecision,
    ServiceKind,
)
from dockerizer.scanner.discovery import ProjectFiles


@dataclass(frozen=True)
class ServiceSpec:
    kind: ServiceKind
    label: str
    image: str
    port: int
    python_deps: frozenset[str]
    node_deps: frozenset[str]
    env_hints: tuple[str, ...]
    image_re: re.Pattern[str]


SPECS: dict[ServiceKind, ServiceSpec] = {
    ServiceKind.POSTGRES: ServiceSpec(
        ServiceKind.POSTGRES,
        "PostgreSQL",
        "postgres:17-alpine",
        5432,
        frozenset(
            {"psycopg", "psycopg2", "psycopg2-binary", "psycopg-binary", "asyncpg", "pg8000"}
        ),
        frozenset({"pg", "postgres", "pg-promise"}),
        ("POSTGRES_", "PG"),
        re.compile(r"^(?:[\w./-]+/)?(postgres|postgis|timescaledb|pgvector)(:|$)"),
    ),
    ServiceKind.MYSQL: ServiceSpec(
        ServiceKind.MYSQL,
        "MySQL",
        "mysql:8.4",
        3306,
        frozenset({"mysqlclient", "pymysql", "aiomysql", "mysql-connector-python", "asyncmy"}),
        frozenset({"mysql", "mysql2"}),
        ("MYSQL_",),
        re.compile(r"^(?:[\w./-]+/)?(mysql|mariadb)(:|$)"),
    ),
    ServiceKind.REDIS: ServiceSpec(
        ServiceKind.REDIS,
        "Redis",
        "redis:7-alpine",
        6379,
        frozenset({"redis", "django-redis", "aioredis", "rq", "celery[redis]"}),
        frozenset({"redis", "ioredis", "bull", "bullmq"}),
        ("REDIS_",),
        re.compile(r"^(?:[\w./-]+/)?(redis|valkey)(:|$)"),
    ),
    ServiceKind.RABBITMQ: ServiceSpec(
        ServiceKind.RABBITMQ,
        "RabbitMQ",
        "rabbitmq:4-alpine",
        5672,
        frozenset({"pika", "aio-pika", "aiormq", "celery[librabbitmq]", "librabbitmq"}),
        frozenset({"amqplib", "amqp-connection-manager"}),
        ("RABBITMQ_", "AMQP_"),
        re.compile(r"^(?:[\w./-]+/)?rabbitmq(:|$)"),
    ),
}

REAL_ENV_NAMES = frozenset({".env", ".env.local", ".env.production", ".env.prod"})
CONFIG_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".ini", ".toml", ".cfg", ".yaml", ".yml")
SKIP_CONFIG = (
    "node_modules/",
    "/tests/",
    "/test/",
    "migrations/versions/",
    "/.github/",
    "/.gitlab",
)
DJANGO_ENGINE_RE = re.compile(r"django\.db\.backends\.(postgresql(?:_psycopg2)?|mysql|sqlite3)")
DJANGO_HOST_RE = re.compile(r"[\"']HOST[\"']\s*:\s*[\"']([^\"']+)[\"']")
DB_URL_VARS = ("DATABASE_URL", "DB_URL", "SQLALCHEMY_DATABASE_URI", "DATABASE_URI")


def env_kind_hint(name: str, kinds_with_deps: set[ServiceKind]) -> list[ServiceKind]:
    found = [s.kind for s in SPECS.values() if name.startswith(s.env_hints)]
    if not found and (name in DB_URL_VARS or name.startswith(("DB_", "DATABASE_"))):
        found = [k for k in (ServiceKind.POSTGRES, ServiceKind.MYSQL) if k in kinds_with_deps]
    return found


class InfrastructureAnalyzer:
    def __init__(
        self,
        files: ProjectFiles,
        apps: list[Application],
        env_files: list[str],
        compose_files: list[str],
    ) -> None:
        self.files = files
        self.apps = apps
        self.env_files = env_files
        self.compose_files = compose_files
        self.evidence: dict[ServiceKind, list[Evidence]] = {k: [] for k in SPECS}
        self.images: dict[ServiceKind, str] = {}
        self.schemes: dict[ServiceKind, str] = {}
        self.notes: list[str] = []

    def _app_for(self, rel: str) -> str | None:
        best: Application | None = None
        for app in self.apps:
            inside = app.path == "." or rel.startswith(app.path + "/")
            if inside and (best is None or len(app.path) > len(best.path)):
                best = app
        if best is None or (best.path == "." and len(self.apps) > 1):
            return None
        return best.name

    def _add(self, kind: ServiceKind, evidence: Evidence) -> None:
        self.evidence[kind].append(evidence)

    def analyze(self) -> list[InfraService]:
        self._dependencies()
        self._env_files()
        self._code_and_config()
        self._compose()
        self._env_names()
        services: list[InfraService] = []
        for kind, spec in SPECS.items():
            evidence = self.evidence[kind]
            if not evidence:
                continue
            decision, reason = self._decide(evidence)
            strong_local = [
                e
                for e in evidence
                if e.strength is EvidenceStrength.STRONG and e.points_to is ServiceDecision.LOCAL
            ]
            host_candidate = (
                decision is ServiceDecision.LOCAL
                and bool(strong_local)
                and all(e.host_machine for e in strong_local)
            )
            if host_candidate:
                reason = (
                    "O .env aponta para esta máquina (localhost): pode ser um serviço já "
                    "instalado no host ou um container a criar."
                )
            services.append(
                InfraService(
                    kind=kind,
                    decision=decision,
                    evidence=evidence,
                    used_by=self._used_by(kind, evidence),
                    image=self.images.get(kind, spec.image),
                    reason=reason,
                    url_scheme=self.schemes.get(kind),
                    host_candidate=host_candidate,
                )
            )
        return services

    # -- coleta ---------------------------------------------------------------------------
    def _dependencies(self) -> None:
        for app in self.apps:
            deps = set(app.dependencies)
            for spec in SPECS.values():
                pool = spec.python_deps if app.language is Language.PYTHON else spec.node_deps
                matched = sorted(deps & pool)
                if matched:
                    self._add(
                        spec.kind,
                        Evidence(
                            description=f"Dependência {', '.join(matched)}",
                            source=f"{app.path}",
                            strength=EvidenceStrength.WEAK,
                            app=app.name,
                        ),
                    )

    def _url_evidence(
        self, text: str, source: str, strength: EvidenceStrength, app: str | None
    ) -> None:
        for url in find_urls(text):
            if url.service is None:
                continue
            kind = ServiceKind(url.service)
            local = url.is_local
            if local is None:
                continue
            target = ServiceDecision.LOCAL if local else ServiceDecision.EXTERNAL
            where = "local" if local else "externo"
            self._add(
                kind,
                Evidence(
                    description=f"URL {url.raw_scheme}:// aponta para host {where} ({url.host})",
                    source=source,
                    strength=strength,
                    points_to=target,
                    app=app,
                    host_machine=is_host_machine(url.host),
                ),
            )
            if local and kind not in self.schemes:
                self.schemes[kind] = url.raw_scheme

    def _env_files(self) -> None:
        for rel in self.env_files:
            name = PurePosixPath(rel).name
            strength = (
                EvidenceStrength.STRONG if name in REAL_ENV_NAMES else EvidenceStrength.MEDIUM
            )
            values = parse_env_file(self.files.read(rel))
            text = "\n".join(values.values())
            self._url_evidence(text, rel, strength, self._app_for(rel))
            self._host_vars(values, rel, strength, self._app_for(rel))

    def _host_vars(
        self, values: dict[str, str], source: str, strength: EvidenceStrength, app: str | None
    ) -> None:
        """``DB_HOST=localhost`` e afins: o host também decide, mesmo sem URL completa."""
        kinds_with_deps = {k for k, ev in self.evidence.items() if ev}
        for name, value in values.items():
            host = value.strip()
            if not name.endswith("_HOST") or not host or any(c in host for c in "${}"):
                continue
            for kind in env_kind_hint(name, kinds_with_deps):
                machine = is_host_machine(host)
                local = machine or "." not in host or host == "host.docker.internal"
                self._add(
                    kind,
                    Evidence(
                        description=f"{name}={host} ({'local' if local else 'externo'})",
                        source=source,
                        strength=strength,
                        points_to=ServiceDecision.LOCAL if local else ServiceDecision.EXTERNAL,
                        app=app,
                        host_machine=machine,
                    ),
                )

    def _code_and_config(self) -> None:
        for app in self.apps:
            for rel in self.files.under(app.path, CONFIG_SUFFIXES):
                if any(part in "/" + rel for part in SKIP_CONFIG):
                    continue
                if PurePosixPath(rel).name in ("pnpm-lock.yaml", "package-lock.json"):
                    continue
                if rel in self.compose_files:
                    continue
                text = self.files.read(rel)
                self._url_evidence(text, rel, EvidenceStrength.MEDIUM, app.name)
                if app.has_framework(Framework.DJANGO) and rel.endswith(".py"):
                    self._django(text, rel, app.name)

    def _django(self, text: str, rel: str, app: str) -> None:
        for engine in DJANGO_ENGINE_RE.findall(text):
            if engine == "sqlite3":
                self.notes.append(
                    f"SQLite configurado em {rel}: os dados ficam dentro do container e não são "
                    "persistidos entre recriações."
                )
                continue
            kind = ServiceKind.MYSQL if engine == "mysql" else ServiceKind.POSTGRES
            host_match = DJANGO_HOST_RE.search(text)
            points_to: ServiceDecision | None = None
            description = f"ENGINE django.db.backends.{engine}"
            if host_match:
                host = host_match.group(1)
                is_local = host in ("localhost", "127.0.0.1") or "." not in host
                points_to = ServiceDecision.LOCAL if is_local else ServiceDecision.EXTERNAL
                description += f" com HOST '{host}'"
            self._add(
                kind,
                Evidence(
                    description=description,
                    source=rel,
                    strength=EvidenceStrength.MEDIUM,
                    points_to=points_to,
                    app=app,
                ),
            )

    def _compose(self) -> None:
        for rel in self.compose_files:
            try:
                data: Any = yaml.safe_load(self.files.read(rel))
            except yaml.YAMLError:
                self.notes.append(f"{rel} não pôde ser lido (YAML inválido).")
                continue
            services = data.get("services") if isinstance(data, dict) else None
            if not isinstance(services, dict):
                continue
            for name, service in services.items():
                image = service.get("image") if isinstance(service, dict) else None
                if not isinstance(image, str):
                    continue
                for spec in SPECS.values():
                    if spec.image_re.match(image):
                        self.images.setdefault(spec.kind, image)
                        self._add(
                            spec.kind,
                            Evidence(
                                description=f"Serviço '{name}' ({image}) em Compose existente",
                                source=rel,
                                strength=EvidenceStrength.STRONG,
                                points_to=ServiceDecision.LOCAL,
                            ),
                        )

    def _env_names(self) -> None:
        kinds_with_deps = {k for k, ev in self.evidence.items() if ev}
        for app in self.apps:
            for name in app.env_vars:
                for kind in env_kind_hint(name, kinds_with_deps):
                    self._add(
                        kind,
                        Evidence(
                            description=f"Código lê a variável {name}",
                            source=app.path,
                            strength=EvidenceStrength.MEDIUM,
                            app=app.name,
                        ),
                    )

    # -- decisão --------------------------------------------------------------------------
    @staticmethod
    def _decide(evidence: list[Evidence]) -> tuple[ServiceDecision, str]:
        for strength in (EvidenceStrength.STRONG, EvidenceStrength.MEDIUM):
            targets = {
                e.points_to for e in evidence if e.strength is strength and e.points_to is not None
            }
            if len(targets) > 1:
                return (
                    ServiceDecision.UNDETERMINED,
                    "Evidências conflitantes (host local e externo) no mesmo nível de confiança.",
                )
            if targets:
                target = targets.pop()
                label = "forte" if strength is EvidenceStrength.STRONG else "média"
                if target is ServiceDecision.LOCAL:
                    return target, f"Configuração aponta para serviço local (evidência {label})."
                return target, (
                    f"Configuração aponta para serviço externo (evidência {label}); "
                    "nenhum container será criado."
                )
        return (
            ServiceDecision.UNDETERMINED,
            "Há indícios de uso, mas nenhuma configuração indica se o serviço é local ou externo.",
        )

    def _used_by(self, kind: ServiceKind, evidence: list[Evidence]) -> list[str]:
        names = {e.app for e in evidence if e.app}
        if not names:
            names = {
                a.name for a in self.apps if a.language is Language.PYTHON or a.role != "frontend"
            }
        return sorted(n for n in names if n is not None)


def has_usage_signal(service: InfraService) -> bool:
    """Indica se há mais que uma dependência (ex.: código lê a variável de conexão)."""
    return any(e.strength is not EvidenceStrength.WEAK for e in service.evidence)
