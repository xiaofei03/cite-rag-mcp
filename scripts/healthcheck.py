from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
ZOTERO = "http://127.0.0.1:23119"

if PYTHON.exists() and Path(sys.executable).resolve() != PYTHON.resolve():
    os.execv(str(PYTHON), [str(PYTHON), *sys.argv])

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def status(ok: bool, label: str, detail: str = "") -> None:
    marker = "OK" if ok else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"[{marker}] {label}{suffix}")


def local_pandoc_dir() -> Path | None:
    executable = "pandoc.exe" if os.name == "nt" else "pandoc"
    for candidate in (ROOT / "tools").glob(f"pandoc-*/pandoc-*/bin/{executable}"):
        return candidate.parent
    return None


def env() -> dict[str, str]:
    value = dict(os.environ)
    local = local_pandoc_dir()
    if local:
        value["PATH"] = f"{local}{os.pathsep}{value.get('PATH', '')}"
    return value


def check_pandoc() -> bool:
    executable = shutil.which("pandoc", path=env().get("PATH"))
    if not executable:
        status(False, "Pandoc", "not found on PATH")
        return False
    completed = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False)
    first = completed.stdout.splitlines()[0] if completed.stdout else executable
    status(completed.returncode == 0, "Pandoc", first)
    return completed.returncode == 0


def check_zotero() -> bool:
    try:
        with urllib.request.urlopen(f"{ZOTERO}/connector/ping", timeout=5) as response:
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
        status(True, "Zotero Connector", "running")
    except Exception as exc:
        status(False, "Zotero Connector", str(exc))
        return False

    try:
        query = urllib.parse.urlencode({"limit": 1})
        request = urllib.request.Request(
            f"{ZOTERO}/api/users/0/items?{query}",
            headers={"Zotero-API-Version": "3"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8", errors="replace")
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}: {body[:120]}")
        status(True, "Zotero Local API", "enabled")
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") or str(exc)
        status(False, "Zotero Local API", detail)
        return False
    except Exception as exc:
        status(False, "Zotero Local API", str(exc))
        return False


async def check_mcp() -> bool:
    if not PYTHON.exists():
        status(False, "Python environment", f"missing {PYTHON}")
        return False

    params = StdioServerParameters(command=str(PYTHON), args=[str(ROOT / "server.py")], env=env())
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                status(True, "MCP tools", f"{len(tools.tools)} tools")
                result = await session.call_tool("get_selected_zotero_target", {})
                text = getattr(result.content[0], "text", "") if result.content else ""
                status(True, "MCP Zotero read", text.replace("\n", " ")[:160])
                return True
    except Exception as exc:
        status(False, "MCP startup/read", str(exc))
        return False


def main() -> None:
    ok = True
    ok = check_pandoc() and ok
    ok = check_zotero() and ok
    ok = asyncio.run(check_mcp()) and ok
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
