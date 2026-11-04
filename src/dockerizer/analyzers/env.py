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
BASESETTINGS_RE = re.compile(r"^class\s+(\w+)\(\s*[\w.]*BaseSettings\s*\)\s*:", re.MULTILINE)
FIELD_RE = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^=\s][^=]*)")
ALIAS_RE = re.compile(r"\b(?:validation_)?alias\s*=\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']")
ENV_PREFIX_RE = re.compile(r"\benv_prefix\s*=\s*[\"']([A-Za-z0-9_]*)[\"']")
ANNOTATION_NAMES_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Valor padrão literal: os.getenv("X", "valor") / os.environ.get("X", "valor").
PY_ENV_DEFAULT_RE = re.compile(
    r"os\.(?:environ\.get|getenv)\(\s*[\"']([A-Z_][A-Z0-9_]*)[\"']\s*,\s*"
    r"(?:Path\(\s*)?[\"']([^\"']+)[\"']"
)
# Padrão de campo BaseSettings: = "x", = Path("x"), Field(default="x") ou Field("x").
FIELD_DEFAULT_RE = re.compile(
    r"=\s*(?:Path\(\s*|Field\(\s*(?:default\s*=\s*)?(?:Path\(\s*)?)?[\"']([^\"']*)[\"']"
)

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
# Hosts que, num .env usado fora do Docker, significam "serviço instalado nesta máquina".
HOST_MACHINE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_host_machine(host: str | None) -> bool:
    if not host:
        return False
    host = host.strip().strip("[]").lower()
    if host in HOST_MACHINE_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Configurações *sobre* segredos que não são segredos: duração, tamanho, algoritmo, flags e
# caminhos (ex.: ACCESS_TOKEN_EXPIRE_MINUTES, PASSWORD_MIN_LENGTH, JWT_ALGORITHM).
NOT_SECRET_RE = re.compile(
    r"_(MINUTES|SECONDS|HOURS|DAYS|TTL|EXPIRE|EXPIRES|EXPIRY|EXPIRATION|LIFETIME|TIMEOUT|"
    r"LENGTH|MIN_LENGTH|MAX_LENGTH|ALGORITHM|ALGORITHMS|ENABLED|DISABLED|REQUIRED|HEADER|"
    r"PATH|FILE|DIR|COOKIE_NAME|PARAM|ROUNDS)$|_MUST_CHANGE_PASSWORD$",
    re.IGNORECASE,
)


def is_sensitive(name: str, value: str | None = None) -> bool:
    if value and "://" in value and "@" in value:
        return True  # URL com credenciais, qualquer que seja o nome
    return bool(SENSITIVE_RE.search(name)) and not NOT_SECRET_RE.search(name)


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


def python_env_defaults(source: str) -> dict[str, str]:
    """Valores padrão literais das variáveis lidas pelo código (sem executar nada)."""
    defaults = {k: v for k, v in _basesettings_fields(source).items() if v is not None}
    for name, value in PY_ENV_DEFAULT_RE.findall(source):
        if _valid(name):
            defaults.setdefault(name, value)
    return defaults


def _basesettings_fields(source: str) -> dict[str, str | None]:
    """Campos de classes ``BaseSettings``, com o ``env_prefix`` aplicado.

    Campos cujo tipo é outra classe ``BaseSettings`` do mesmo arquivo são subconfigurações
    aninhadas (lidas com o prefixo da própria classe), não variáveis de ambiente.
    """
    names: dict[str, str | None] = {}
    lines = source.splitlines()
    matches = list(BASESETTINGS_RE.finditer(source))
    settings_classes = {m.group(1) for m in matches}
    for match in matches:
        start = source.count("\n", 0, match.start()) + 1
        body: list[str] = []
        for line in lines[start:]:
            if line.strip() and not line[0].isspace():
                break
            body.append(line)
        prefix_match = ENV_PREFIX_RE.search("\n".join(body))
        prefix = prefix_match.group(1) if prefix_match else ""
        for field_name, annotation, text in _class_fields(body):
            if field_name in ("model_config", "Config"):
                continue
            if settings_classes & set(ANNOTATION_NAMES_RE.findall(annotation)):
                continue
            alias = ALIAS_RE.search(text)
            name = alias.group(1).upper() if alias else f"{prefix}{field_name}".upper()
            if name not in IGNORED_ENV:
                names[name] = _field_default(text)
    return names


def _field_default(text: str) -> str | None:
    """Padrão literal do campo: só a primeira atribuição conta (ignora ``alias="..."``)."""
    after_annotation = text.split(":", 1)[1] if ":" in text else ""
    index = after_annotation.find("=")
    if index < 0:
        return None
    match = FIELD_DEFAULT_RE.match(after_annotation[index:])
    return match.group(1) if match else None


def _class_fields(body: list[str]) -> list[tuple[str, str, str]]:
    """(nome, anotação, texto completo) de cada campo no primeiro nível do corpo da classe.

    O texto inclui as linhas de continuação (``Field(...)`` em várias linhas); docstrings
    são ignoradas para que linhas como ``Uso:`` não pareçam campos.
    """
    fields: list[tuple[str, str, list[str]]] = []
    current: list[str] | None = None
    indent: str | None = None
    open_quote: str | None = None
    for line in body:
        stripped = line.strip()
        if open_quote is not None:
            if open_quote in stripped:
                open_quote = None
            continue
        if not stripped or stripped.startswith("#"):
            continue
        mark = stripped[:3]
        if mark in ('"""', "'''"):
            if stripped.count(mark) == 1:
                open_quote = mark
            continue
        line_indent = line[: len(line) - len(line.lstrip())]
        if indent is None:
            indent = line_indent
        if line_indent != indent:
            if current is not None and len(line_indent) > len(indent):
                current.append(stripped)
            continue
        match = FIELD_RE.match(line)
        current = None
        if match:
            current = [stripped]
            fields.append((match.group(2), match.group(3), current))
    return [(name, annotation, " ".join(text)) for name, annotation, text in fields]


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
