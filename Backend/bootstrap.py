#!/usr/bin/env python3
"""
Bootstrap helper for COSMIC Backend VM setup.

This script is meant to be the first thing run on a Linux VM after cloning
the backend repo. It currently handles Python readiness, pip availability,
virtual environment creation, backend dependency installation, and WhatsApp
bridge dependency setup. It is intentionally structured so future setup steps
can be added without turning it into an unmaintainable script.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


MIN_PYTHON = (3, 10)
BACKEND_ROOT = Path(__file__).resolve().parent
DEFAULT_VENV_PATH = BACKEND_ROOT / ".venv"
DEFAULT_REQUIREMENTS_PATH = BACKEND_ROOT / "requirements.txt"
DEFAULT_BRIDGE_DIR = BACKEND_ROOT / "bridges" / "whatsapp_bridge"
DEFAULT_SYSTEMD_TEMPLATE_DIR = BACKEND_ROOT / "systemd"
DEFAULT_ENV_SEARCH_ROOTS = (
    BACKEND_ROOT,
    BACKEND_ROOT / "bridges",
)
PYTHON_CANDIDATES = [
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
]
PACKAGE_NAMES: Dict[str, Dict[str, str]] = {
    "python": {
        "apt-get": "python3",
        "dnf": "python3",
        "yum": "python3",
        "apk": "python3",
    },
    "pip": {
        "apt-get": "python3-pip",
        "dnf": "python3-pip",
        "yum": "python3-pip",
        "apk": "py3-pip",
    },
    "venv": {
        "apt-get": "python3-venv",
        "dnf": "python3",
        "yum": "python3",
        "apk": "py3-virtualenv",
    },
    "nodejs": {
        "apt-get": "nodejs",
        "dnf": "nodejs",
        "yum": "nodejs",
        "apk": "nodejs",
    },
    "npm": {
        "apt-get": "npm",
        "dnf": "npm",
        "yum": "npm",
        "apk": "npm",
    },
}


class BootstrapError(RuntimeError):
    pass


def log(message: str) -> None:
    print("[bootstrap] {0}".format(message))


def command_str(command: Sequence[str]) -> str:
    return shlex.join(list(command))


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def run(
    command: Sequence[str],
    *,
    use_sudo: bool = False,
    capture_output: bool = False,
    check: bool = True,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    full_command = list(command)
    if use_sudo and not is_root():
        sudo_path = shutil.which("sudo")
        if not sudo_path:
            raise BootstrapError(
                "System package install requires root or sudo. Missing sudo for command: {0}".format(
                    command_str(command)
                )
            )
        full_command = [sudo_path] + full_command

    log("Running: {0}".format(command_str(full_command)))
    return subprocess.run(
        full_command,
        check=check,
        text=True,
        capture_output=capture_output,
        cwd=str(cwd) if cwd else None,
    )


def detect_package_manager() -> Optional[str]:
    for manager in ("apt-get", "dnf", "yum", "apk"):
        if shutil.which(manager):
            return manager
    return None


def executable_version(command: Sequence[str]) -> Optional[str]:
    try:
        result = run(command, capture_output=True)
    except (BootstrapError, subprocess.CalledProcessError, FileNotFoundError):
        return None
    return (result.stdout or "").strip() or None


def install_system_packages(manager: str, packages: Iterable[str]) -> None:
    package_list = [pkg for pkg in packages if pkg]
    if not package_list:
        return

    if manager == "apt-get":
        run(["apt-get", "update"], use_sudo=True)
        run(["apt-get", "install", "-y", *package_list], use_sudo=True)
        return
    if manager == "dnf":
        run(["dnf", "install", "-y", *package_list], use_sudo=True)
        return
    if manager == "yum":
        run(["yum", "install", "-y", *package_list], use_sudo=True)
        return
    if manager == "apk":
        run(["apk", "add", "--no-cache", *package_list], use_sudo=True)
        return

    raise BootstrapError("Unsupported package manager: {0}".format(manager))


def example_target_path(example_path: Path) -> Path:
    if example_path.name == ".env.example":
        return example_path.with_name(".env")
    if example_path.name.endswith(".env.example"):
        return example_path.with_name(example_path.name[: -len(".example")])
    raise BootstrapError("Unsupported env example naming: {0}".format(example_path))


def discover_env_example_files(search_roots: Sequence[Path]) -> List[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name != ".env.example" and not path.name.endswith(".env.example"):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            discovered.append(resolved)
    return discovered


def ensure_env_files(search_roots: Sequence[Path]) -> List[Path]:
    created: list[Path] = []
    for example_path in discover_env_example_files(search_roots):
        target_path = example_target_path(example_path)
        if target_path.exists():
            log("Environment file already exists: {0}".format(target_path))
            continue

        shutil.copy2(example_path, target_path)
        created.append(target_path)
        log("Created env file from template: {0}".format(target_path))
    return created


def version_for(executable: str) -> Optional[Tuple[int, int, int]]:
    try:
        result = run(
            [
                executable,
                "-c",
                "import sys; print('{0}.{1}.{2}'.format(*sys.version_info[:3]))",
            ],
            capture_output=True,
        )
    except (BootstrapError, subprocess.CalledProcessError, FileNotFoundError):
        return None

    raw = (result.stdout or "").strip()
    if not raw:
        return None

    parts = raw.split(".")
    if len(parts) != 3:
        return None

    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def is_supported_python(version: Optional[Tuple[int, int, int]]) -> bool:
    return bool(version and version[:2] >= MIN_PYTHON)


def find_supported_python() -> Optional[str]:
    current = version_for(sys.executable)
    if is_supported_python(current):
        return sys.executable

    seen = set([sys.executable])
    for candidate in PYTHON_CANDIDATES:
        path = shutil.which(candidate)
        if not path or path in seen:
            continue
        seen.add(path)
        if is_supported_python(version_for(path)):
            return path
    return None


def maybe_reexec_with_supported_python() -> None:
    current_version = sys.version_info[:3]
    if current_version[:2] >= MIN_PYTHON:
        log(
            "Using Python {0}.{1}.{2} from {3}".format(
                current_version[0], current_version[1], current_version[2], sys.executable
            )
        )
        return

    supported = find_supported_python()
    if supported and Path(supported).resolve() != Path(sys.executable).resolve():
        log("Switching to supported interpreter: {0}".format(supported))
        os.execv(supported, [supported, str(Path(__file__).resolve()), *sys.argv[1:]])

    raise BootstrapError(
        "Python {0}.{1}.{2} is too old. COSMIC Backend needs Python {3}.{4}+.".format(
            current_version[0],
            current_version[1],
            current_version[2],
            MIN_PYTHON[0],
            MIN_PYTHON[1],
        )
    )


def ensure_python3_available() -> None:
    try:
        maybe_reexec_with_supported_python()
        return
    except BootstrapError:
        manager = detect_package_manager()
        if not is_linux() or not manager:
            raise

        package_name = PACKAGE_NAMES["python"].get(manager)
        if not package_name:
            raise

        log("Supported Python not found. Installing {0} via {1}.".format(package_name, manager))
        install_system_packages(manager, [package_name])

    supported = find_supported_python()
    if not supported:
        raise BootstrapError(
            "Installed python3 package, but no Python {0}.{1}+ interpreter was found. "
            "Use a newer distro/repository or install Python manually.".format(
                MIN_PYTHON[0], MIN_PYTHON[1]
            )
        )

    if Path(supported).resolve() != Path(sys.executable).resolve():
        log("Restarting bootstrap with {0}".format(supported))
        os.execv(supported, [supported, str(Path(__file__).resolve()), *sys.argv[1:]])


def has_pip() -> bool:
    try:
        run([sys.executable, "-m", "pip", "--version"], capture_output=True)
        return True
    except (BootstrapError, subprocess.CalledProcessError):
        return False


def ensure_pip() -> None:
    if has_pip():
        result = run([sys.executable, "-m", "pip", "--version"], capture_output=True)
        log("pip available: {0}".format((result.stdout or "").strip()))
        return

    log("pip not found. Trying ensurepip.")
    try:
        run([sys.executable, "-m", "ensurepip", "--upgrade"])
    except (BootstrapError, subprocess.CalledProcessError):
        manager = detect_package_manager()
        if not is_linux() or not manager:
            raise BootstrapError("Failed to install pip with ensurepip and no supported package manager was found.")

        package_name = PACKAGE_NAMES["pip"].get(manager)
        if not package_name:
            raise BootstrapError("No pip package mapping for package manager: {0}".format(manager))

        log("Installing pip package via {0}: {1}".format(manager, package_name))
        install_system_packages(manager, [package_name])

    if not has_pip():
        raise BootstrapError("pip is still unavailable after installation attempts.")


def has_venv_module() -> bool:
    try:
        run([sys.executable, "-m", "venv", "--help"], capture_output=True)
        return True
    except (BootstrapError, subprocess.CalledProcessError):
        return False


def ensure_venv_support() -> None:
    if has_venv_module():
        log("venv module available.")
        return

    manager = detect_package_manager()
    if not is_linux() or not manager:
        raise BootstrapError("Python venv module is missing and no supported Linux package manager was found.")

    package_name = PACKAGE_NAMES["venv"].get(manager)
    if not package_name:
        raise BootstrapError("No venv package mapping for package manager: {0}".format(manager))

    log("Installing venv support via {0}: {1}".format(manager, package_name))
    install_system_packages(manager, [package_name])

    if not has_venv_module():
        raise BootstrapError("Python venv support is still unavailable after installation attempts.")


def venv_python_path(venv_path: Path) -> Path:
    return venv_path / "bin" / "python"


def ensure_virtualenv(venv_path: Path) -> None:
    if venv_python_path(venv_path).exists():
        log("Virtual environment already exists at {0}".format(venv_path))
        return

    log("Creating virtual environment at {0}".format(venv_path))
    run([sys.executable, "-m", "venv", str(venv_path)])


def upgrade_venv_pip(venv_path: Path) -> None:
    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        raise BootstrapError("Missing venv python executable at {0}".format(python_path))

    run([str(python_path), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])


def install_python_requirements(venv_path: Path, requirements_path: Path) -> None:
    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        raise BootstrapError("Missing venv python executable at {0}".format(python_path))
    if not requirements_path.exists():
        raise BootstrapError("Missing requirements file at {0}".format(requirements_path))

    log("Installing backend Python dependencies from {0}".format(requirements_path))
    run([str(python_path), "-m", "pip", "install", "-r", str(requirements_path)])


def has_node() -> bool:
    return executable_version(["node", "--version"]) is not None


def has_npm() -> bool:
    return executable_version(["npm", "--version"]) is not None


def ensure_node_toolchain() -> None:
    node_version = executable_version(["node", "--version"])
    npm_version = executable_version(["npm", "--version"])
    if node_version and npm_version:
        log("Node available: {0}".format(node_version))
        log("npm available: {0}".format(npm_version))
        return

    manager = detect_package_manager()
    if not is_linux() or not manager:
        raise BootstrapError("Node.js/npm missing and no supported Linux package manager was found.")

    packages: list[str] = []
    if not node_version:
        package_name = PACKAGE_NAMES["nodejs"].get(manager)
        if not package_name:
            raise BootstrapError("No Node.js package mapping for package manager: {0}".format(manager))
        packages.append(package_name)
    if not npm_version:
        package_name = PACKAGE_NAMES["npm"].get(manager)
        if not package_name:
            raise BootstrapError("No npm package mapping for package manager: {0}".format(manager))
        packages.append(package_name)

    log("Installing Node.js toolchain via {0}: {1}".format(manager, ", ".join(packages)))
    install_system_packages(manager, packages)

    if not has_node() or not has_npm():
        raise BootstrapError("Node.js/npm are still unavailable after installation attempts.")


def load_package_json(package_json: Path) -> dict:
    try:
        return json.loads(package_json.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BootstrapError("Missing package.json at {0}".format(package_json)) from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError("Invalid package.json at {0}: {1}".format(package_json, exc)) from exc


def install_whatsapp_bridge_dependencies(bridge_dir: Path) -> None:
    package_json = bridge_dir / "package.json"
    if not bridge_dir.exists():
        raise BootstrapError("WhatsApp bridge directory does not exist: {0}".format(bridge_dir))
    if not package_json.exists():
        raise BootstrapError("Missing WhatsApp bridge package.json at {0}".format(package_json))

    ensure_node_toolchain()
    package_data = load_package_json(package_json)

    package_lock = bridge_dir / "package-lock.json"
    node_modules = bridge_dir / "node_modules"
    install_command = ["npm", "install"]
    if package_lock.exists() and not node_modules.exists():
        install_command = ["npm", "ci"]

    log("Installing WhatsApp bridge dependencies in {0}".format(bridge_dir))
    run(install_command, check=True, capture_output=False, cwd=bridge_dir)

    scripts = package_data.get("scripts") if isinstance(package_data.get("scripts"), dict) else {}
    if "build" in scripts:
        log("Running WhatsApp bridge build script in {0}".format(bridge_dir))
        run(["npm", "run", "build"], check=True, capture_output=False, cwd=bridge_dir)


def install_systemd_units(template_dir: Path, *, enable_units: bool = False) -> List[str]:
    if not is_linux():
        raise BootstrapError("Systemd install currently targets Linux VMs only.")
    if shutil.which("systemctl") is None:
        raise BootstrapError("systemctl not found. This host does not appear to use systemd.")
    if not template_dir.exists():
        raise BootstrapError("Systemd template directory does not exist: {0}".format(template_dir))

    templates = sorted(path for path in template_dir.glob("*.example") if path.is_file())
    if not templates:
        raise BootstrapError("No systemd template files found in {0}".format(template_dir))

    installed_names: list[str] = []
    with tempfile.TemporaryDirectory(prefix="cosmic-systemd-") as temp_dir:
        temp_path = Path(temp_dir)
        for template in templates:
            rendered_name = template.name[: -len(".example")]
            rendered_path = temp_path / rendered_name
            rendered_text = template.read_text(encoding="utf-8").replace("<BACKEND_ROOT>", str(BACKEND_ROOT))
            rendered_path.write_text(rendered_text, encoding="utf-8")
            run(
                ["install", "-m", "644", str(rendered_path), "/etc/systemd/system/{0}".format(rendered_name)],
                use_sudo=True,
            )
            installed_names.append(rendered_name)

    run(["systemctl", "daemon-reload"], use_sudo=True)

    if enable_units and installed_names:
        preferred_units = [name for name in installed_names if name.endswith(".target")] or installed_names
        run(["systemctl", "enable", *preferred_units], use_sudo=True)

    return installed_names


def doctor(
    venv_path: Path,
    requirements_path: Path,
    bridge_dir: Path,
    systemd_template_dir: Path,
    env_search_roots: Sequence[Path],
) -> None:
    manager = detect_package_manager()
    current_version = sys.version_info[:3]
    current_supported = current_version[:2] >= MIN_PYTHON
    env_examples = discover_env_example_files(env_search_roots)

    print("COSMIC Backend bootstrap doctor")
    print("  platform           : {0}".format(sys.platform))
    print("  current python     : {0}.{1}.{2} ({3})".format(
        current_version[0], current_version[1], current_version[2], sys.executable
    ))
    print("  python supported   : {0}".format("yes" if current_supported else "no"))
    print("  package manager    : {0}".format(manager or "not found"))
    print("  pip available      : {0}".format("yes" if has_pip() else "no"))
    print("  venv available     : {0}".format("yes" if has_venv_module() else "no"))
    print("  target venv        : {0}".format(venv_path))
    print("  venv exists        : {0}".format("yes" if venv_python_path(venv_path).exists() else "no"))
    print("  requirements file  : {0}".format(requirements_path if requirements_path.exists() else "missing"))
    print("  node available     : {0}".format(executable_version(["node", "--version"]) or "no"))
    print("  npm available      : {0}".format(executable_version(["npm", "--version"]) or "no"))
    print("  bridge dir         : {0}".format(bridge_dir if bridge_dir.exists() else "missing"))
    print("  bridge package     : {0}".format(
        (bridge_dir / "package.json") if (bridge_dir / "package.json").exists() else "missing"
    ))
    print("  env search roots   : {0}".format(", ".join(str(path) for path in env_search_roots)))
    print("  env templates      : {0}".format(len(env_examples)))
    print("  systemd templates  : {0}".format(systemd_template_dir if systemd_template_dir.exists() else "missing"))

    supported = find_supported_python()
    if supported and Path(supported).resolve() != Path(sys.executable).resolve():
        print("  alternate python   : {0}".format(supported))


def setup_env_files(search_roots: Sequence[Path]) -> List[Path]:
    created = ensure_env_files(search_roots)
    if not created:
        log("No new env files were created from templates.")
    return created


def setup_python(venv_path: Path, requirements_path: Path) -> None:
    if not is_linux():
        raise BootstrapError("This bootstrap flow currently targets Linux VMs only.")

    ensure_python3_available()
    ensure_pip()
    ensure_venv_support()
    ensure_virtualenv(venv_path)
    upgrade_venv_pip(venv_path)
    install_python_requirements(venv_path, requirements_path)


def setup_whatsapp_bridge(bridge_dir: Path) -> None:
    if not is_linux():
        raise BootstrapError("This bootstrap flow currently targets Linux VMs only.")

    install_whatsapp_bridge_dependencies(bridge_dir)


def bootstrap(venv_path: Path, requirements_path: Path, bridge_dir: Path, env_search_roots: Sequence[Path]) -> None:
    setup_env_files(env_search_roots)
    setup_python(venv_path, requirements_path)
    setup_whatsapp_bridge(bridge_dir)

    print("")
    print("Bootstrap complete")
    print("  python : {0}".format(sys.executable))
    print("  venv   : {0}".format(venv_path))
    print("  deps   : {0}".format(requirements_path))
    print("  bridge : {0}".format(bridge_dir))
    print("  next   : source {0}/bin/activate".format(venv_path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap COSMIC Backend on a Linux VM.")
    parser.add_argument(
        "--venv-path",
        default=str(DEFAULT_VENV_PATH),
        help="Virtual environment path. Default: %(default)s",
    )
    parser.add_argument(
        "--requirements",
        default=str(DEFAULT_REQUIREMENTS_PATH),
        help="Backend requirements file path. Default: %(default)s",
    )
    parser.add_argument(
        "--bridge-dir",
        default=str(DEFAULT_BRIDGE_DIR),
        help="WhatsApp bridge directory. Default: %(default)s",
    )
    parser.add_argument(
        "--systemd-template-dir",
        default=str(DEFAULT_SYSTEMD_TEMPLATE_DIR),
        help="Directory containing systemd *.example templates. Default: %(default)s",
    )
    parser.add_argument(
        "--env-search-root",
        action="append",
        default=[],
        help="Directory root to scan recursively for *.env.example templates. Can be passed multiple times.",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="Check current bootstrap prerequisites without changing the system.")
    subparsers.add_parser(
        "bootstrap",
        help="Install missing prerequisites, create the backend virtual environment, install Python deps, and install WhatsApp bridge deps.",
    )
    subparsers.add_parser(
        "setup-python",
        help="Prepare the Python runtime, create/update the backend virtual environment, and install backend Python dependencies.",
    )
    subparsers.add_parser(
        "setup-whatsapp-bridge",
        help="Install Node.js bridge dependencies in bridges/whatsapp_bridge.",
    )
    subparsers.add_parser(
        "setup-env",
        help="Create missing env files from committed *.env.example templates.",
    )
    install_systemd_parser = subparsers.add_parser(
        "install-systemd",
        help="Install systemd unit templates into /etc/systemd/system and reload systemd.",
    )
    install_systemd_parser.add_argument(
        "--enable",
        action="store_true",
        help="Enable the installed units (prefers the target unit when present).",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "bootstrap"
    venv_path = Path(args.venv_path).expanduser().resolve()
    requirements_path = Path(args.requirements).expanduser().resolve()
    bridge_dir = Path(args.bridge_dir).expanduser().resolve()
    systemd_template_dir = Path(args.systemd_template_dir).expanduser().resolve()
    env_search_roots = [
        Path(item).expanduser().resolve()
        for item in (args.env_search_root or [])
    ] or list(DEFAULT_ENV_SEARCH_ROOTS)

    try:
        if command == "doctor":
            doctor(venv_path, requirements_path, bridge_dir, systemd_template_dir, env_search_roots)
        elif command == "setup-env":
            setup_env_files(env_search_roots)
        elif command == "setup-python":
            setup_python(venv_path, requirements_path)
        elif command == "setup-whatsapp-bridge":
            setup_whatsapp_bridge(bridge_dir)
        elif command == "install-systemd":
            installed = install_systemd_units(
                systemd_template_dir,
                enable_units=bool(getattr(args, "enable", False)),
            )
            print("Installed systemd units:")
            for unit_name in installed:
                print("  - {0}".format(unit_name))
        else:
            bootstrap(venv_path, requirements_path, bridge_dir, env_search_roots)
    except BootstrapError as exc:
        print("Bootstrap failed: {0}".format(exc), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            "Bootstrap failed while running: {0} (exit={1})".format(
                command_str(exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)]),
                exc.returncode,
            ),
            file=sys.stderr,
        )
        return exc.returncode or 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
