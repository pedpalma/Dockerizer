"""Identificação de variáveis de ambiente e URLs de conexão."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

PY_ENV_PATTERNS = (
    re.compile(r"os\.(?:environ\.get|getenv)\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"),
    re.compile(r"os\.environ\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]"),
    re.compile(r"os\.environ\.setdefault\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"),
    # django-environ: env("X"), env.str("X"), env.bool("X")... e python-decouple: config("X")
    re.compile(r"\benv(?:\.\w+)?\(\s*[\"']([A-Z_][A-Z0-9_]*)[\"']"),
    re.compile(r"\bconfig\(\s*[\"']([A-Z_][A-Z0-9_]*)[\"']"),
)
NODE_ENV_PATTERNS = (
    re.compile(r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"process\.env\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\s*\]"),
    re.compile(r"import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)"),
)
BASESETTINGS_RE = re.compile(r"^class\s+\w+\(\s*[\w.]*BaseSettings\s*\)\s*:", re.MULTILINE)
FIELD_RE = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*[^=]")

# Variáveis do sistema/runtime que não fazem sentido em .env.example.
IGNORED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "PWD",
        "SHELL",
        "TERM",
        "LANG",
        "HOSTNAME",
        "TMPDIR",
        "CI",
        "PYTHONPATH",
        "NODE_ENV",
        "PORT",
        "HOST",
        "DEV",
        "PROD",
        "MODE",
        "SSR",
        "BASE_URL",
    }
)
SENSITIVE_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|_PASS$|^PASS$|TOKEN|API_?KEY|PRIVATE|CREDENTIAL|_KEY$|^KEY$|"
    r"SALT|SIGNATURE|AUTH|DSN|CERT)",
    re.IGNORECASE,
)
URL_RE = re.compile(
    r"\b((?:postgres(?:ql)?|mysql|mariadb|redis|rediss|amqps?)(?:\+[a-z0-9]+)?://[^\s'\"`<>]+)",
    re.IGNORECASE,
)
ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
PUBLIC_PREFIXES = ("NEXT_PUBLIC_", "VITE_", "REACT_APP_")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"})


def is_sensitive(name: str, value: str | None = None) -> bool:
    if SENSITIVE_RE.search(name):
        return True
    return bool(value and "://" in value and "@" in value)


def is_public_build_var(name: str) -> bool:
    return name.startswith(PUBLIC_PREFIXES)


def parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = ENV_LINE_RE.match(line)
        if not match:
            continue
        name, raw = match.groups()
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    return values


def _valid(name: str) -> bool:
    return name.upper() == name and name not in IGNORED_ENV and len(name) > 1


def python_env_refs(source: str) -> set[str]:
    names: set[str] = set()
    for pattern in PY_ENV_PATTERNS:
        names.update(m for m in pattern.findall(source) if _valid(m))
    names.update(_basesettings_fields(source))
    return names


def _basesettings_fields(source: str) -> set[str]:
    names: set[str] = set()
    lines = source.splitlines()
    for match in BASESETTINGS_RE.finditer(source):
        start = source.count("\n", 0, match.start()) + 1
        indent: str | None = None
        for line in lines[start:]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            current_indent = line[: len(line) - len(line.lstrip())]
            if not current_indent:
                break
            if indent is None:
                indent = current_indent
            if current_indent != indent:
                continue
            field_match = FIELD_RE.match(line)
            if field_match and field_match.group(2) not in ("model_config", "Config"):
                name = field_match.group(2).upper()
                if name not in IGNORED_ENV:
                    names.add(name)
    return names


def node_env_refs(source: str) -> set[str]:
    names: set[str] = set()
    for pattern in NODE_ENV_PATTERNS:
        names.update(m for m in pattern.findall(source) if _valid(m))
    return names


@dataclass(frozen=True)
class ConnectionUrl:
    scheme: str
    host: str | None
    port: int | None
    raw_scheme: str

    @property
    def service(self) -> str | None:
        base = self.scheme
        if base in ("postgres", "postgresql"):
            return "postgres"
        if base in ("mysql", "mariadb"):
            return "mysql"
        if base in ("redis", "rediss"):
            return "redis"
        if base in ("amqp", "amqps"):
            return "rabbitmq"
        return None

    @property
    def is_local(self) -> bool | None:
        """``True`` para host local/nome de serviço, ``False`` para host externo, ``None`` se
        o host é dinâmico (interpolação) ou ausente."""
        host = self.host
        if not host:
            return None
        if any(ch in host for ch in "${}%<>"):
            return None
        if host.lower() in LOCAL_HOSTS:
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return "." not in host
        return address.is_loopback


def parse_connection_url(url: str) -> ConnectionUrl | None:
    try:
        parts = urlsplit(url)
        raw_scheme = parts.scheme.lower()
        scheme = raw_scheme.split("+", 1)[0]
        host = parts.hostname
        try:
            port = parts.port
        except ValueError:
            port = None
    except ValueError:
        return None
    return ConnectionUrl(scheme=scheme, host=host, port=port, raw_scheme=raw_scheme)


def find_urls(text: str) -> list[ConnectionUrl]:
    urls: list[ConnectionUrl] = []
    for raw in URL_RE.findall(text):
        parsed = parse_connection_url(raw.rstrip(",;)"))
        if parsed and parsed.service:
            urls.append(parsed)
    return urls
