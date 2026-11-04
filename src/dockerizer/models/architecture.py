"""Modelo de domínio da arquitetura detectada.

Separa claramente:
- **Aplicações** (código do projeto que vira imagem Docker);
- **Processos** (formas de executar uma aplicação: web, worker, scheduler, frontend estático);
- **Serviços de infraestrutura** (bancos, cache, filas), que só são criados localmente
  quando há evidência de que o projeto precisa deles.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Language(StrEnum):
    PYTHON = "python"
    NODE = "node"


class PackageManager(StrEnum):
    PIP = "pip"
    POETRY = "poetry"
    UV = "uv"
    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"


class Framework(StrEnum):
    FASTAPI = "fastapi"
    FLASK = "flask"
    DJANGO = "django"
    CELERY = "celery"
    ALEMBIC = "alembic"
    NEXTJS = "nextjs"
    REACT = "react"
    VITE = "vite"
    EXPRESS = "express"
    NODE = "node"


class AppRole(StrEnum):
    BACKEND = "backend"
    FRONTEND = "frontend"
    WORKER = "worker"


class ProcessKind(StrEnum):
    WEB = "web"
    WORKER = "worker"
    SCHEDULER = "scheduler"
    STATIC = "static"


class Confidence(StrEnum):
    DETECTED = "detectado"
    SUGGESTED = "sugerido"
    CONFIRMED = "confirmado"


class Mode(StrEnum):
    PRODUCTION = "production"
    DEVELOPMENT = "development"


class Command(BaseModel):
    argv: list[str]
    source: str
    confidence: Confidence = Confidence.DETECTED

    def display(self) -> str:
        import shlex

        return shlex.join(self.argv)


class Process(BaseModel):
    """Um processo executável de uma aplicação."""

    name: str
    kind: ProcessKind
    command: Command | None = None
    dev_command: Command | None = None
    port: int | None = None
    dev_port: int | None = None
    candidates: list[Command] = Field(default_factory=list)
    celery_app: str | None = None
    health_path: str | None = None  # rota HTTP para o healthcheck (ex.: /health)

    def command_for(self, mode: Mode) -> Command | None:
        if mode is Mode.DEVELOPMENT and self.dev_command is not None:
            return self.dev_command
        return self.command

    def port_for(self, mode: Mode) -> int | None:
        if mode is Mode.DEVELOPMENT and self.dev_port is not None:
            return self.dev_port
        return self.port


class DataDir(BaseModel):
    """Diretório em que a aplicação grava arquivos que precisam sobreviver ao container."""

    path: str  # caminho absoluto dentro do container
    source: str
    variable: str | None = None
    volume: str = ""


class Migration(BaseModel):
    tool: str
    command: list[str]
    source: str


class Application(BaseModel):
    name: str
    path: str
    language: Language
    language_version: str
    package_manager: PackageManager
    frameworks: list[Framework] = Field(default_factory=list)
    role: AppRole
    dependencies: list[str] = Field(default_factory=list)
    processes: list[Process] = Field(default_factory=list)
    migration: Migration | None = None
    env_vars: list[str] = Field(default_factory=list)
    build_args: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    existing_dockerfile: str | None = None
    # Contexto/target de build declarados num Compose existente (padrão: a pasta da aplicação).
    build_context: str | None = None
    build_target: str | None = None
    data_dirs: list[DataDir] = Field(default_factory=list)

    # Python
    dependency_files: list[str] = Field(default_factory=list)
    dev_dependency_files: list[str] = Field(default_factory=list)
    inline_dependencies: list[str] = Field(default_factory=list)
    has_build_system: bool = False
    python_path: str | None = None

    # Node
    lockfile: str | None = None
    yarn_berry: bool = False
    build_command: list[str] | None = None
    static_output_dir: str | None = None
    next_standalone: bool = False  # next.config com output: "standalone"
    has_public_dir: bool = False

    def has_framework(self, framework: Framework) -> bool:
        return framework in self.frameworks

    def process(self, name: str) -> Process | None:
        return next((p for p in self.processes if p.name == name), None)


class ServiceKind(StrEnum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    REDIS = "redis"
    RABBITMQ = "rabbitmq"


class ServiceDecision(StrEnum):
    LOCAL = "local"
    EXTERNAL = "externo"
    UNDETERMINED = "indeterminado"
    DISCARDED = "descartado"
    # Serviço que já roda na máquina (ex.: PostgreSQL instalado): acessado via
    # host.docker.internal, sem container nem volume.
    HOST = "host"


class EvidenceStrength(StrEnum):
    WEAK = "fraca"
    MEDIUM = "media"
    STRONG = "forte"


class Evidence(BaseModel):
    description: str
    source: str
    strength: EvidenceStrength
    points_to: ServiceDecision | None = None
    app: str | None = None
    host_machine: bool = False  # aponta para localhost/127.0.0.1 (esta máquina)


class InfraService(BaseModel):
    kind: ServiceKind
    decision: ServiceDecision
    evidence: list[Evidence] = Field(default_factory=list)
    used_by: list[str] = Field(default_factory=list)
    image: str
    reason: str = ""
    url_scheme: str | None = None
    # Decidido como local só porque o .env real aponta para localhost: pode ser um serviço
    # já instalado na máquina, e não um container a criar. Exige confirmação.
    host_candidate: bool = False
    # Container novo escolhido no lugar de um serviço que já roda no host (dados não copiados).
    replaces_host: bool = False


class EnvVar(BaseModel):
    name: str
    sources: list[str] = Field(default_factory=list)
    apps: list[str] = Field(default_factory=list)
    sensitive: bool = False
    example_value: str | None = None


class QuestionKind(StrEnum):
    COMMAND = "comando"
    CHOICE = "escolha"
    TEXT = "texto"


class Question(BaseModel):
    """Informação que não pôde ser identificada com segurança e precisa de confirmação."""

    id: str
    kind: QuestionKind
    prompt: str
    target: str
    default: str | None = None
    choices: list[str] = Field(default_factory=list)
    help: str = ""


class ProxyRoute(BaseModel):
    location: str
    kind: str  # proxy, estatico, redirecionamento, outro
    target: str
    app: str | None = None
    port: int | None = None


class ReverseProxy(BaseModel):
    """Configuração de nginx encontrada no projeto. É reportada, nunca reproduzida."""

    path: str
    server_names: list[str] = Field(default_factory=list)
    listens: list[str] = Field(default_factory=list)
    tls: bool = False
    routes: list[ProxyRoute] = Field(default_factory=list)
    settings: list[str] = Field(default_factory=list)


class ExistingDockerFile(BaseModel):
    path: str
    kind: str
    managed: bool = False


class ProjectAnalysis(BaseModel):
    root: str
    project_name: str
    applications: list[Application] = Field(default_factory=list)
    services: list[InfraService] = Field(default_factory=list)
    env_vars: list[EnvVar] = Field(default_factory=list)
    env_files: list[str] = Field(default_factory=list)
    existing_docker_files: list[ExistingDockerFile] = Field(default_factory=list)
    init_scripts: list[str] = Field(default_factory=list)
    reverse_proxies: list[ReverseProxy] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def local_services(self) -> list[InfraService]:
        return [s for s in self.services if s.decision is ServiceDecision.LOCAL]

    def host_services(self) -> list[InfraService]:
        return [s for s in self.services if s.decision is ServiceDecision.HOST]

    def service(self, kind: ServiceKind) -> InfraService | None:
        return next((s for s in self.services if s.kind is kind), None)

    def app(self, name: str) -> Application | None:
        return next((a for a in self.applications if a.name == name), None)
