"""Tradução dos textos fixos do Typer usados na ajuda e em erros de uso.

O Typer não oferece tradução oficial; os textos são substituídos de forma defensiva: se a
estrutura interna mudar em versões futuras, a ajuda apenas permanece em inglês.
"""

from __future__ import annotations

import importlib
from typing import Any

import typer.rich_utils as rich_utils

RICH_TEXTS = {
    "ARGUMENTS_PANEL_TITLE": "Argumentos",
    "OPTIONS_PANEL_TITLE": "Opções",
    "COMMANDS_PANEL_TITLE": "Comandos",
    "ERRORS_PANEL_TITLE": "Erro",
    "ABORTED_TEXT": "Operação cancelada.",
    "DEFAULT_STRING": "[padrão: {}]",
    "REQUIRED_LONG_STRING": "[obrigatório]",
    "RICH_HELP": "Use [blue]'{command_path} {help_option}'[/] para ajuda.",
}
HELP_TEXT = "Mostra esta mensagem e sai."
ERROR_TRANSLATIONS = (
    ("No such option", "Opção inexistente"),
    ("No such command", "Comando inexistente"),
    ("Did you mean", "Você quis dizer"),
    ("Missing argument", "Argumento ausente"),
    ("Missing option", "Opção ausente"),
    ("Invalid value for", "Valor inválido para"),
    ("Invalid value", "Valor inválido"),
    ("Got unexpected extra arguments", "Argumentos extras inesperados"),
    ("Got unexpected extra argument", "Argumento extra inesperado"),
    ("requires an argument", "exige um valor"),
    ("is not a valid integer", "não é um número inteiro válido"),
    ("is not a valid int range", "não é um número inteiro válido"),
)
USAGE_PREFIX = "Uso: "

_installed = False


def _patch_help_option() -> None:
    try:
        core: Any = importlib.import_module("typer._click.core")
    except ImportError:  # pragma: no cover - depende da versão do Typer
        return
    command_cls = getattr(core, "Command", None)
    original = getattr(command_cls, "get_help_option", None)
    if command_cls is None or original is None:  # pragma: no cover
        return

    def get_help_option(self: Any, ctx: Any) -> Any:
        option = original(self, ctx)
        if option is not None and hasattr(option, "help"):
            option.help = HELP_TEXT
        return option

    command_cls.get_help_option = get_help_option


def _patch_usage() -> None:
    try:
        formatting: Any = importlib.import_module("typer._click.formatting")
    except ImportError:  # pragma: no cover
        return
    formatter_cls = getattr(formatting, "HelpFormatter", None)
    original = getattr(formatter_cls, "write_usage", None)
    if formatter_cls is None or original is None:  # pragma: no cover
        return

    def write_usage(self: Any, prog: str, args: str = "", prefix: str | None = None) -> None:
        original(self, prog, args, USAGE_PREFIX if prefix is None else prefix)

    formatter_cls.write_usage = write_usage


def _patch_errors() -> None:
    try:
        exceptions: Any = importlib.import_module("typer._click.exceptions")
    except ImportError:  # pragma: no cover
        return
    base = getattr(exceptions, "UsageError", None)
    if base is None:  # pragma: no cover
        return
    # Subclasses (ex.: NoSuchOption) sobrescrevem format_message: cada uma é traduzida.
    base.format_message = _translated(base.format_message)
    classes = list(base.__subclasses__())
    while classes:
        cls = classes.pop()
        classes.extend(cls.__subclasses__())
        if "format_message" in vars(cls):
            cls.format_message = _translated(vars(cls)["format_message"])


def _translated(original: Any) -> Any:
    def format_message(self: Any) -> str:
        message = str(original(self))
        for english, portuguese in ERROR_TRANSLATIONS:
            message = message.replace(english, portuguese)
        return message

    return format_message


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    for name, value in RICH_TEXTS.items():
        if hasattr(rich_utils, name):
            setattr(rich_utils, name, value)
    _patch_help_option()
    _patch_usage()
    _patch_errors()
