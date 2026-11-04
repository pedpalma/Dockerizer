"""Constantes compartilhadas. Versões de imagens ficam centralizadas aqui para facilitar atualização."""

from typing import Final

TOOL_DIR: Final = ".dockerizer"
MANIFEST_FILE: Final = "manifest.json"
BACKUPS_DIR: Final = "backups"
REPORTS_DIR: Final = "reports"

COMPOSE_FILE: Final = "compose.yaml"
# Cópia do .env para os containers, com localhost trocado por host.docker.internal.
HOST_ENV_FILE: Final = ".env.docker"
COMPOSE_FILE_NAMES: Final = frozenset(
    {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"}
)

MANAGED_LABEL: Final = "com.dockerizer.managed"
# Sufixo do projeto Compose: projetos já dockerizados usam o nome do diretório como projeto,
# e compartilhar esse nome faria o Compose tratar os containers deles como nossos.
COMPOSE_PROJECT_SUFFIX: Final = "-dockerizer"


def compose_project_name(project_name: str) -> str:
    return f"{project_name}{COMPOSE_PROJECT_SUFFIX}"


COMPOSE_PROJECT_LABEL: Final = "com.docker.compose.project"
COMPOSE_VOLUME_LABEL: Final = "com.docker.compose.volume"

DEFAULT_PYTHON_VERSION: Final = "3.12"
DEFAULT_NODE_VERSION: Final = "22"
UV_IMAGE: Final = "ghcr.io/astral-sh/uv:0.12"
NGINX_IMAGE: Final = "nginxinc/nginx-unprivileged:1.27-alpine"
NGINX_PORT: Final = 8080

# Diretórios que nunca são analisados (dependências, caches, artefatos de build, VCS).
IGNORED_DIRS: Final = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        TOOL_DIR,
        "node_modules",
        ".venv",
        "venv",
        "env",
        ".env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "site-packages",
        "dist",
        "build",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
        "coverage",
        "htmlcov",
        ".idea",
        ".vscode",
    }
)

MAX_SCAN_FILES: Final = 20_000
MAX_SCAN_DEPTH: Final = 8
MAX_READ_BYTES: Final = 1_000_000
