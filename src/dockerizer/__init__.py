"""Dockerizer: containerização automatizada de projetos existentes."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Fonte única da versão: [project].version do pyproject.toml.
    __version__ = version("dockerizer")
except PackageNotFoundError:  # executado direto do código-fonte, sem instalação
    __version__ = "0+desconhecida"
