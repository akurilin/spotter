"""Manage the per-user macOS LaunchAgent that runs the scanner on a schedule."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from errors import ConfigError, LaunchAgentError
from paths import app_log_dir, app_log_path


def launch_agent_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return LaunchAgent settings with conservative defaults."""
    agent_config = config.get("launch_agent", {})
    if not isinstance(agent_config, dict):
        raise ConfigError("launch_agent config must be an object.")
    return agent_config


def launch_agent_label(config: dict[str, Any]) -> str:
    """Return the configured launchd label for this scanner."""
    label = launch_agent_config(config).get("label", "com.example.waparser")
    if not isinstance(label, str) or not label.strip():
        raise ConfigError("launch_agent.label must be a non-empty string.")
    return label.strip()


def launch_agent_domain() -> str:
    """Return the launchctl domain for the current GUI user session."""
    return f"gui/{os.getuid()}"


def launch_agent_plist_path(config: dict[str, Any]) -> Path:
    """Return the per-user LaunchAgents plist path for this scanner."""
    return Path.home() / "Library" / "LaunchAgents" / f"{launch_agent_label(config)}.plist"


def launch_agent_service_name(config: dict[str, Any]) -> str:
    """Return the launchctl service target for this scanner."""
    return f"{launch_agent_domain()}/{launch_agent_label(config)}"


def project_root() -> Path:
    """Return the project directory that contains this script."""
    return Path(__file__).resolve().parent


def local_python_path() -> Path:
    """Return the current Python executable and require it to be inside the local venv."""
    python_path = Path(sys.executable)
    if not python_path.is_absolute():
        python_path = (Path.cwd() / python_path).resolve()

    expected_venv = project_root() / ".venv"
    if python_path.parent.parent != expected_venv:
        raise LaunchAgentError(
            "Install the LaunchAgent with the local virtualenv Python: ./.venv/bin/python wap_alerts.py install-agent"
        )
    return python_path


def build_launch_agent_plist(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Build the launchd property list for periodic scanner execution."""
    agent_config = launch_agent_config(config)
    interval_seconds = int(agent_config.get("start_interval_seconds", 1800))
    if interval_seconds <= 0:
        raise ConfigError("launch_agent.start_interval_seconds must be positive.")

    root = project_root()
    logs_dir = app_log_dir(config)
    stdout_path = logs_dir / "launchd.out.log"
    stderr_path = logs_dir / "launchd.err.log"

    return {
        "Label": launch_agent_label(config),
        "ProgramArguments": [
            str(local_python_path()),
            str(root / "wap_alerts.py"),
            "--config",
            str(config_path),
            "run",
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": bool(agent_config.get("run_at_load", True)),
        "StartInterval": interval_seconds,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "EnvironmentVariables": {
            "PYTHONUNBUFFERED": "1",
        },
    }


def install_launch_agent(config: dict[str, Any], config_path: Path) -> int:
    """Install or update the per-user macOS LaunchAgent."""
    app_log_dir(config).mkdir(parents=True, exist_ok=True)

    plist_path = launch_agent_plist_path(config)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_data = build_launch_agent_plist(config, config_path)
    atomic_write_plist(plist_path, plist_data)

    subprocess.run(["plutil", "-lint", str(plist_path)], check=True)
    bootout_launch_agent(config)
    subprocess.run(["launchctl", "bootstrap", launch_agent_domain(), str(plist_path)], check=True)

    print(f"Installed LaunchAgent {launch_agent_label(config)}.")
    print(f"Plist: {plist_path}")
    print(f"Logs: {app_log_dir(config)}")
    return 0


def uninstall_launch_agent(config: dict[str, Any]) -> int:
    """Unload and remove the per-user macOS LaunchAgent."""
    plist_path = launch_agent_plist_path(config)
    bootout_launch_agent(config)

    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed LaunchAgent plist: {plist_path}")
    else:
        print(f"LaunchAgent plist was already absent: {plist_path}")
    return 0


def show_launch_agent_status(config: dict[str, Any]) -> int:
    """Print the current launchd status for this scanner."""
    label = launch_agent_label(config)
    plist_path = launch_agent_plist_path(config)
    service_name = launch_agent_service_name(config)
    result = subprocess.run(["launchctl", "print", service_name], capture_output=True, text=True, check=False)

    print(f"Label: {label}")
    print(f"Plist: {plist_path}")
    print(f"Plist exists: {plist_path.exists()}")
    print(f"App log: {app_log_path(config)}")
    if result.returncode == 0:
        print("Loaded: yes")
    else:
        print("Loaded: no")
        if result.stderr.strip() and "Could not find service" not in result.stderr:
            print(f"launchctl: {result.stderr.strip()}")
    return 0


def bootout_launch_agent(config: dict[str, Any]) -> None:
    """Unload the LaunchAgent if launchd currently knows about it."""
    plist_path = launch_agent_plist_path(config)
    service_result = subprocess.run(
        ["launchctl", "bootout", launch_agent_service_name(config)],
        capture_output=True,
        text=True,
        check=False,
    )
    if service_result.returncode == 0:
        return

    if plist_path.exists():
        subprocess.run(
            ["launchctl", "bootout", launch_agent_domain(), str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )


def atomic_write_plist(path: Path, data: dict[str, Any]) -> None:
    """Write a plist atomically using XML format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        plistlib.dump(data, handle, fmt=plistlib.FMT_XML, sort_keys=False)
        temp_name = handle.name
    os.replace(temp_name, path)
