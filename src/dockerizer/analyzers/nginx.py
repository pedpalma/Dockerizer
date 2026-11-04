"""Leitura de configurações nginx do projeto (apenas para informar; nada é reproduzido).

Projetos que rodam fora do Docker costumam ter um nginx na frente (proxy reverso, TLS,
arquivos estáticos). O Dockerizer não gera esse proxy: ele lista as rotas e ajustes
encontrados e aponta qual aplicação atende cada ``proxy_pass``, para que o nginx do host
seja apontado para as portas publicadas pelo Compose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from dockerizer.analyzers.env import is_host_machine
from dockerizer.models.architecture import (
    Application,
    ProcessKind,
    ProjectAnalysis,
    ProxyRoute,
    ReverseProxy,
)
from dockerizer.scanner.discovery import ProjectFiles

NGINX_DIRS = frozenset({"nginx", "sites-available", "sites-enabled", "conf.d"})
# Ajustes que mudam o comportamento da aplicação e se perdem sem o proxy.
RELEVANT_SETTINGS = (
    "client_max_body_size",
    "proxy_read_timeout",
    "proxy_send_timeout",
    "auth_basic",
    "limit_req",
    "limit_conn",
    "gzip",
    "add_header",
    "real_ip_header",
    "set_real_ip_from",
)
# Comentários só começam no início de um token e fora de aspas.
TOKEN_RE = re.compile(
    r"""\s*(?:("(?:\\.|[^"])*"|'(?:\\.|[^'])*')|(\#[^\n]*)|([{};])|([^\s{};]+))"""
)


@dataclass
class Directive:
    name: str
    args: list[str]
    children: list[Directive] = field(default_factory=list)

    def find(self, name: str) -> list[Directive]:
        return [c for c in self.children if c.name == name]

    def walk(self) -> list[Directive]:
        found = []
        for child in self.children:
            found.append(child)
            found.extend(child.walk())
        return found


def parse(text: str) -> Directive:
    """Parser mínimo da sintaxe nginx (diretivas, argumentos e blocos)."""
    root = Directive("", [])
    stack = [root]
    words: list[str] = []
    for match in TOKEN_RE.finditer(text):
        quoted, comment, punct, word = match.groups()
        if comment is not None:
            continue
        if quoted is not None:
            words.append(quoted[1:-1])
        elif word is not None:
            words.append(word)
        elif punct == ";":
            if words:
                stack[-1].children.append(Directive(words[0], words[1:]))
            words = []
        elif punct == "{":
            block = Directive(words[0] if words else "", words[1:])
            stack[-1].children.append(block)
            stack.append(block)
            words = []
        elif punct == "}" and len(stack) > 1:
            stack.pop()
            words = []
    return root


def find_nginx_configs(files: ProjectFiles) -> list[str]:
    found = []
    for rel in files.files:
        path = PurePosixPath(rel)
        in_nginx_dir = bool(set(path.parts[:-1]) & NGINX_DIRS)
        if (
            path.name == "nginx.conf"
            or (in_nginx_dir and path.suffix in (".conf", ""))
            or (path.suffix == ".conf" and "nginx" in path.name)
        ):
            found.append(rel)
    return found


def _app_for_port(port: int | None, apps: list[Application]) -> str | None:
    if port is None:
        return None
    for app in apps:
        for process in app.processes:
            if process.kind in (ProcessKind.WEB, ProcessKind.STATIC) and process.port == port:
                return app.name
    return None


def _target_port(target: str, upstreams: dict[str, list[str]]) -> tuple[str, int | None]:
    """Resolve ``proxy_pass`` (inclusive upstreams) para (descrição, porta local)."""
    try:
        parts = urlsplit(target if "://" in target else f"http://{target}")
        host, port = parts.hostname, parts.port
    except ValueError:
        return target, None
    if host and host in upstreams:
        servers = upstreams[host]
        resolved = ", ".join(servers)
        for server in servers:
            _, port = _target_port(server, {})
            if port is not None:
                return f"{target} (upstream: {resolved})", port
        return f"{target} (upstream: {resolved})", None
    if target.startswith("unix:") or (parts.netloc.startswith("unix:")):
        return target, None
    if host and not is_host_machine(host) and host != "0.0.0.0":
        return target, None
    return target, port


def _app_for_root(root: str, apps: list[Application]) -> str | None:
    parts = PurePosixPath(root).parts
    for app in sorted(apps, key=lambda a: -len(a.path)):
        if app.path == ".":
            continue
        app_parts = PurePosixPath(app.path).parts
        for index in range(len(parts) - len(app_parts) + 1):
            if tuple(parts[index : index + len(app_parts)]) == app_parts:
                return app.name
    return None


def analyze_config(rel: str, text: str, apps: list[Application]) -> list[ReverseProxy]:
    tree = parse(text)
    upstreams = {
        d.args[0]: [s.args[0] for s in d.find("server") if s.args]
        for d in tree.walk()
        if d.name == "upstream" and d.args
    }
    proxies = []
    for server in (d for d in tree.walk() if d.name == "server" and d.children):
        listens = [" ".join(d.args) for d in server.find("listen")]
        names = [n for d in server.find("server_name") for n in d.args if n != "_"]
        tls = any("ssl" in item.split() for item in listens) or any(
            d.name == "ssl_certificate" for d in server.walk()
        )
        proxy = ReverseProxy(path=rel, server_names=names, listens=listens, tls=tls)
        for location in (d for d in server.walk() if d.name == "location"):
            path = " ".join(location.args)
            for directive in location.children:
                if directive.name in ("proxy_pass", "uwsgi_pass", "fastcgi_pass", "grpc_pass"):
                    target, port = _target_port(
                        directive.args[0] if directive.args else "", upstreams
                    )
                    proxy.routes.append(
                        ProxyRoute(
                            location=path,
                            kind="proxy",
                            target=f"{directive.name} {target}",
                            app=_app_for_port(port, apps),
                            port=port,
                        )
                    )
                elif directive.name in ("root", "alias") and directive.args:
                    proxy.routes.append(
                        ProxyRoute(
                            location=path,
                            kind="estatico",
                            target=f"{directive.name} {directive.args[0]}",
                            app=_app_for_root(directive.args[0], apps),
                        )
                    )
                elif directive.name == "return" and directive.args:
                    proxy.routes.append(
                        ProxyRoute(
                            location=path,
                            kind="redirecionamento",
                            target="return " + " ".join(directive.args),
                        )
                    )
        for directive in server.find("return"):
            proxy.routes.append(
                ProxyRoute(
                    location="(servidor)",
                    kind="redirecionamento",
                    target="return " + " ".join(directive.args),
                )
            )
        seen: set[str] = set()
        for directive in server.walk():
            if directive.name in RELEVANT_SETTINGS:
                entry = f"{directive.name} {' '.join(directive.args)}".strip()
                if entry not in seen:
                    seen.add(entry)
                    proxy.settings.append(entry)
        if proxy.routes or proxy.listens:
            proxies.append(proxy)
    return proxies


def analyze_nginx(files: ProjectFiles, analysis: ProjectAnalysis) -> None:
    for rel in find_nginx_configs(files):
        text = files.read(rel)
        if "server" not in text and "location" not in text:
            continue
        analysis.reverse_proxies.extend(analyze_config(rel, text, analysis.applications))
    if analysis.reverse_proxies:
        analysis.warnings.append(
            "Configuração nginx encontrada: proxy reverso, TLS, domínios e ajustes do nginx "
            "não são reproduzidos no Compose. Mantenha o nginx do host apontando para as portas "
            "publicadas (veja a tabela 'Proxy reverso existente')."
        )
