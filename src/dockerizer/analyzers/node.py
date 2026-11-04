"""Analisador de aplicações JavaScript/TypeScript (Node.js, Next.js, React; npm, pnpm, yarn)."""

from __future__ import annotations

import json
import re
from typing import Any

from dockerizer.analyzers.env import is_public_build_var, node_env_refs
from dockerizer.constants import DEFAULT_NODE_VERSION, NGINX_PORT
from dockerizer.models.architecture import (
    Application,
    AppRole,
    Command,
    Confidence,
    Framework,
    Language,
    PackageManager,
    Process,
    ProcessKind,
)
from dockerizer.scanner.discovery import ProjectFiles, join

SERVER_FRAMEWORKS = ("express", "fastify", "koa", "@nestjs/core", "@hapi/hapi", "hono")
SOURCE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".jsx", ".tsx")
LISTEN_RE = re.compile(r"\.listen\(\s*(\d{2,5})")
PORT_FALLBACK_RE = re.compile(r"PORT\s*(?:\|\||\?\?)\s*['\"]?(\d{2,5})")
SCRIPT_PORT_RE = re.compile(r"(?:-p|--port)[= ](\d{2,5})")
OUTDIR_RE = re.compile(r"outDir\s*:\s*['\"]([^'\"]+)['\"]")
LOCKFILES = (
    ("pnpm-lock.yaml", PackageManager.PNPM),
    ("yarn.lock", PackageManager.YARN),
    ("package-lock.json", PackageManager.NPM),
    ("npm-shrinkwrap.json", PackageManager.NPM),
)
SKIP_PARTS = ("node_modules/", "dist/", "build/", ".next/", "coverage/", "test/", "tests/")


def choose_node_version(engines: str | None, pinned: str | None) -> str:
    for spec in (pinned, engines):
        if not spec:
            continue
        match = re.search(r"(\d+)", spec.strip().lstrip("v"))
        if not match:
            continue
        major = int(match.group(1))
        if spec is engines and (">=" in spec or "^" in spec or ">" in spec):
            return str(max(major, int(DEFAULT_NODE_VERSION)))
        return str(major)
    return DEFAULT_NODE_VERSION


def run_script(manager: PackageManager, script: str, *extra: str) -> list[str]:
    argv = [manager.value, "run", script]
    if extra:
        argv += ["--", *extra] if manager is PackageManager.NPM else list(extra)
    return argv


class NodeAnalyzer:
    def __init__(self, files: ProjectFiles, app_dir: str, name: str) -> None:
        self.files = files
        self.app_dir = app_dir
        self.name = name

    def _has(self, name: str) -> bool:
        return self.files.has(join(self.app_dir, name))

    def _read(self, name: str) -> str:
        return self.files.read(join(self.app_dir, name))

    def analyze(self) -> tuple[Application | None, str | None]:
        """Retorna a aplicação ou ``(None, motivo)`` quando o package.json não é uma aplicação."""
        try:
            pkg: dict[str, Any] = json.loads(self._read("package.json"))
        except json.JSONDecodeError:
            return None, f"{join(self.app_dir, 'package.json')} inválido (JSON malformado)."
        deps = {**pkg.get("devDependencies", {}), **pkg.get("dependencies", {})}
        runtime_deps = set(pkg.get("dependencies", {}))
        scripts: dict[str, str] = {k: str(v) for k, v in pkg.get("scripts", {}).items()}
        frameworks = self._frameworks(deps, runtime_deps)
        main = pkg.get("main")
        main_exists = isinstance(main, str) and self._has(main.removeprefix("./"))
        if not frameworks and "start" not in scripts and not main_exists:
            return None, (
                f"{join(self.app_dir, 'package.json')} parece conter apenas ferramentas de "
                "desenvolvimento (sem start, main ou framework); ignorado."
            )
        manager, lockfile, evidence = self._package_manager(pkg)
        app = Application(
            name=self.name,
            path=self.app_dir,
            language=Language.NODE,
            language_version=choose_node_version(
                (pkg.get("engines") or {}).get("node"), self._pinned_version()
            ),
            package_manager=manager,
            frameworks=frameworks,
            role=AppRole.FRONTEND
            if Framework.NEXTJS in frameworks or Framework.REACT in frameworks
            else AppRole.BACKEND,
            dependencies=sorted(deps),
            evidence=evidence,
            lockfile=lockfile,
            yarn_berry=self._is_yarn_berry(pkg, manager),
        )
        if pkg.get("workspaces") or self._has("pnpm-workspace.yaml"):
            app.warnings.append(
                "Monorepo com workspaces detectado: o suporte é limitado; revise o Dockerfile."
            )
        if lockfile is None:
            app.warnings.append(
                "Nenhum lockfile encontrado: a instalação não será reprodutível "
                "(recomenda-se versionar o lockfile)."
            )
        sources = [
            rel
            for rel in self.files.under(self.app_dir, SOURCE_SUFFIXES)
            if not any(part in rel for part in SKIP_PARTS)
        ]
        texts = [self.files.read(rel) for rel in sources[:3000]]
        env: set[str] = set()
        for text in texts:
            env |= node_env_refs(text)
        app.env_vars = sorted(env)
        app.build_args = sorted(v for v in env if is_public_build_var(v))
        if Framework.NEXTJS in frameworks:
            self._next(app, scripts)
        elif Framework.REACT in frameworks:
            self._spa(app, scripts)
        else:
            self._server(app, scripts, main if main_exists else None, texts)
        return app, None

    def _pinned_version(self) -> str | None:
        for name in (".nvmrc", ".node-version"):
            if self._has(name):
                return self._read(name).strip() or None
        return None

    def _frameworks(self, deps: dict[str, Any], runtime: set[str]) -> list[Framework]:
        found: list[Framework] = []
        if "next" in deps:
            found.append(Framework.NEXTJS)
        if "react" in deps and "next" not in deps and ("react-dom" in deps or "vite" in deps):
            found.append(Framework.REACT)
        if "vite" in deps:
            found.append(Framework.VITE)
        if "express" in runtime:
            found.append(Framework.EXPRESS)
        if any(f in runtime for f in SERVER_FRAMEWORKS) and Framework.NEXTJS not in found:
            found.append(Framework.NODE)
        return found

    def _package_manager(self, pkg: dict[str, Any]) -> tuple[PackageManager, str | None, list[str]]:
        declared = str(pkg.get("packageManager", ""))
        for lock, manager in LOCKFILES:
            if self._has(lock):
                return manager, lock, [f"{lock} encontrado"]
        for manager in (PackageManager.PNPM, PackageManager.YARN, PackageManager.NPM):
            if declared.startswith(manager.value + "@"):
                return manager, None, [f"packageManager={declared} em package.json"]
        return PackageManager.NPM, None, ["package.json sem lockfile (npm por padrão)"]

    def _is_yarn_berry(self, pkg: dict[str, Any], manager: PackageManager) -> bool:
        if manager is not PackageManager.YARN:
            return False
        declared = str(pkg.get("packageManager", ""))
        match = re.match(r"yarn@(\d+)", declared)
        return self._has(".yarnrc.yml") or bool(match and int(match.group(1)) >= 2)

    def _script_cmd(self, app: Application, scripts: dict[str, str], name: str) -> Command | None:
        if name not in scripts:
            return None
        return Command(
            argv=run_script(app.package_manager, name),
            source=f"package.json scripts.{name} ({scripts[name]})",
        )

    def _next(self, app: Application, scripts: dict[str, str]) -> None:
        process = Process(name="web", kind=ProcessKind.WEB, port=3000, dev_port=3000)
        if "build" in scripts:
            app.build_command = run_script(app.package_manager, "build")
        else:
            app.warnings.append("Next.js sem script 'build' em package.json.")
        process.command = self._script_cmd(app, scripts, "start")
        if process.command is None:
            process.candidates = [
                Command(
                    argv=["npx", "next", "start"],
                    source="Next.js (padrão do framework)",
                    confidence=Confidence.SUGGESTED,
                )
            ]
        else:
            port = SCRIPT_PORT_RE.search(scripts["start"])
            process.port = int(port.group(1)) if port else 3000
        process.dev_command = self._script_cmd(app, scripts, "dev")
        if "dev" in scripts:
            port = SCRIPT_PORT_RE.search(scripts["dev"])
            process.dev_port = int(port.group(1)) if port else 3000
        app.evidence.append("Dependência 'next' em package.json")
        app.processes.append(process)

    def _spa(self, app: Application, scripts: dict[str, str]) -> None:
        is_vite = app.has_framework(Framework.VITE)
        is_cra = "react-scripts" in app.dependencies
        process = Process(name="web", kind=ProcessKind.STATIC, port=NGINX_PORT)
        if "build" in scripts:
            app.build_command = run_script(app.package_manager, "build")
            process.command = Command(
                argv=["nginx", "-g", "daemon off;"],
                source="Build estático servido por nginx (scripts.build)",
            )
        if is_vite:
            config = next(
                (
                    self._read(n)
                    for n in ("vite.config.ts", "vite.config.js", "vite.config.mjs")
                    if self._has(n)
                ),
                "",
            )
            outdir = OUTDIR_RE.search(config)
            app.static_output_dir = outdir.group(1) if outdir else "dist"
            if "dev" in scripts:
                port_match = SCRIPT_PORT_RE.search(scripts["dev"])
                process.dev_port = int(port_match.group(1)) if port_match else 5173
                process.dev_command = Command(
                    argv=run_script(app.package_manager, "dev", "--host", "0.0.0.0"),
                    source=f"package.json scripts.dev ({scripts['dev']})",
                )
            app.evidence.append("React + Vite (SPA)")
        elif is_cra:
            app.static_output_dir = "build"
            if "start" in scripts:
                process.dev_port = 3000
                process.dev_command = self._script_cmd(app, scripts, "start")
            app.evidence.append("React + react-scripts (Create React App)")
        else:
            app.warnings.append(
                "Aplicação React sem Vite/react-scripts: confirme o diretório de saída do build."
            )
        if app.build_command is None:
            app.warnings.append("Aplicação React sem script 'build'; produção não será possível.")
        app.processes.append(process)

    def _server(
        self, app: Application, scripts: dict[str, str], main: str | None, texts: list[str]
    ) -> None:
        port: int | None = None
        for text in texts:
            match = LISTEN_RE.search(text) or PORT_FALLBACK_RE.search(text)
            if match:
                port = int(match.group(1))
                break
        if port is None and any("process.env.PORT" in text for text in texts):
            port = 3000
            app.evidence.append("A aplicação lê process.env.PORT; PORT=3000 será definido")
        process = Process(name="web", kind=ProcessKind.WEB, port=port, dev_port=port)
        if "build" in scripts:
            app.build_command = run_script(app.package_manager, "build")
        process.command = self._script_cmd(app, scripts, "start")
        if process.command is None and main:
            process.command = Command(argv=["node", main], source="package.json main")
        if process.command is None:
            for candidate in ("server.js", "index.js", "app.js", "src/index.js", "src/server.js"):
                if self._has(candidate):
                    process.candidates.append(
                        Command(
                            argv=["node", candidate],
                            source=candidate,
                            confidence=Confidence.SUGGESTED,
                        )
                    )
        process.dev_command = self._script_cmd(app, scripts, "dev")
        app.evidence.append("Aplicação Node.js (servidor)")
        app.processes.append(process)
