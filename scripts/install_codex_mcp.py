from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
DEFAULT_CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
PANDOC_VERSION = "3.10"


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(command))
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def python_executable() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def ensure_python_version() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required. Run this installer with a newer Python.")


def ensure_venv() -> Path:
    ensure_python_version()
    if not python_executable().exists():
        run([sys.executable, "-m", "venv", str(VENV)])
    py = python_executable()
    run([str(py), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(py), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    return py


def pandoc_asset_name() -> str | None:
    machine = platform.machine().lower()
    system = platform.system().lower()
    if system == "darwin":
        arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
        return f"pandoc-{PANDOC_VERSION}-{arch}-macOS.zip"
    if system == "windows":
        if machine not in {"amd64", "x86_64"}:
            print(f"Unsupported Windows CPU for bundled Pandoc download: {platform.machine()}")
            return None
        return f"pandoc-{PANDOC_VERSION}-windows-x86_64.zip"
    return None


def pandoc_bin_dir() -> Path | None:
    local = ROOT / "tools" / f"pandoc-{PANDOC_VERSION}"
    executable = "pandoc.exe" if os.name == "nt" else "pandoc"
    candidates = list(local.glob(f"pandoc-{PANDOC_VERSION}-*/bin/{executable}"))
    if candidates:
        return candidates[0].parent
    return None


def ensure_pandoc() -> Path | None:
    system_pandoc = shutil.which("pandoc")
    if system_pandoc:
        print(f"Found system pandoc: {system_pandoc}")
        return Path(system_pandoc).parent

    asset = pandoc_asset_name()
    if asset is None:
        print("No bundled Pandoc downloader for this OS. Install pandoc manually and keep it on PATH.")
        return None

    existing = pandoc_bin_dir()
    if existing:
        print(f"Found local pandoc: {existing / 'pandoc'}")
        return existing

    target = ROOT / "tools" / f"pandoc-{PANDOC_VERSION}"
    target.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/jgm/pandoc/releases/download/{PANDOC_VERSION}/{asset}"
    archive = target / asset
    print(f"Downloading Pandoc {PANDOC_VERSION}: {url}")
    urllib.request.urlretrieve(url, archive)
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(target)

    found = pandoc_bin_dir()
    if found is None:
        raise SystemExit("Pandoc was downloaded but the executable was not found.")
    return found


def toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_config_block(py: Path, pandoc_dir: Path | None) -> str:
    lines = [
        "[mcp_servers.cite-rag-mcp]",
        f"command = {toml_string(str(py))}",
        f"args = [{toml_string(str(ROOT / 'server.py'))}]",
        "startup_timeout_sec = 20",
    ]
    if pandoc_dir is not None:
        path_value = f"{pandoc_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        lines.extend(
            [
                "",
                "[mcp_servers.cite-rag-mcp.env]",
                f"PATH = {toml_string(path_value)}",
            ]
        )
    return "\n".join(lines) + "\n"


def strip_existing_config(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    skip = False
    for line in lines:
        if line.startswith("[mcp_servers.cite-rag-mcp]") or line.startswith("[mcp_servers.cite-rag-mcp.env]"):
            skip = True
            continue
        if skip and line.startswith("[") and line.endswith("]"):
            skip = False
        if not skip:
            output.append(line)
    return "\n".join(output).rstrip() + "\n"


def update_codex_config(config_path: Path, block: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if original.strip():
        tomllib.loads(original)
    updated = strip_existing_config(original) + "\n" + block
    tomllib.loads(updated)
    config_path.write_text(updated, encoding="utf-8")
    print(f"Updated Codex config: {config_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install cite-rag-mcp into Codex.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CODEX_CONFIG, help="Path to Codex config.toml.")
    parser.add_argument("--no-config", action="store_true", help="Install dependencies only; do not edit Codex config.")
    args = parser.parse_args()

    py = ensure_venv()
    pandoc_dir = ensure_pandoc()
    if not args.no_config:
        update_codex_config(args.config.expanduser(), render_config_block(py, pandoc_dir))

    print("\nInstalled cite-rag-mcp.")
    print("Restart Codex, then run: python scripts/healthcheck.py")


if __name__ == "__main__":
    main()
