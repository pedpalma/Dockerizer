"""Ambiente Jinja2 compartilhado pelos geradores."""

from __future__ import annotations

import json
import shlex
from functools import cache

from jinja2 import Environment, PackageLoader, StrictUndefined


def exec_form(argv: list[str]) -> str:
    """Formato *exec* (JSON) para CMD/ENTRYPOINT, sem shell intermediário."""
    return json.dumps(argv, ensure_ascii=False)


@cache
def environment() -> Environment:
    env = Environment(
        loader=PackageLoader("dockerizer", "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,  # noqa: S701
    )
    env.filters["exec_form"] = exec_form
    env.filters["shell_quote"] = shlex.quote
    env.filters["shell_join"] = shlex.join
    return env


def render(template: str, **context: object) -> str:
    text = environment().get_template(template).render(**context)
    # Normaliza linhas em branco consecutivas produzidas por blocos condicionais.
    lines: list[str] = []
    for line in text.splitlines():
        if not line.strip() and lines and not lines[-1].strip():
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip() + "\n"
