"""Manage the per-user macOS LaunchAgent that runs the scanner on a schedule."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spotter.errors import ConfigError, LaunchAgentError
from spotter.paths import app_log_dir, app_log_path
from spotter.preflight import check_whatsapp_database_access


@dataclass(frozen=True)
class LaunchAgentStatus:
    """Current LaunchAgent installation and runtime status."""

    label: str
    service_name: str
    plist_path: Path
    app_log_path: Path
    current_python_path: Path
    current_python_error: str | None
    plist_exists: bool
    plist_loadable: bool
    plist_error: str | None
    loaded: bool
    loaded_error: str | None
    plist_matches_config: bool | None
    expected_plist_error: str | None
    installed_python_path: str | None
    installed_config_path: str | None

    @property
    def can_install(self) -> bool:
        """Return whether the current Python can generate the expected plist."""
        return self.expected_plist_error is None

    @property
    def is_configured_correctly(self) -> bool:
        """Return whether launchd is loaded with the expected generated plist."""
        return self.loaded and self.plist_matches_config is True and self.expected_plist_error is None


def launch_agent_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return LaunchAgent settings with conservative defaults."""
    agent_config = config.get("launch_agent", {})
    if not isinstance(agent_config, dict):
        raise ConfigError("launch_agent config must be an object.")
    return agent_config


def launch_agent_label(config: dict[str, Any]) -> str:
    """Return the configured launchd label for this scanner."""
    label = launch_agent_config(config).get("label", "com.example.spotter")
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
    """Return the repo root, which is the parent of the spotter package directory."""
    return Path(__file__).resolve().parent.parent


def local_python_path() -> Path:
    """Return the current Python executable and require it to be inside the local venv."""
    python_path = Path(sys.executable)
    if not python_path.is_absolute():
        python_path = (Path.cwd() / python_path).resolve()

    expected_venv = project_root() / ".venv"
    if python_path.parent.parent != expected_venv:
        raise LaunchAgentError(
            "Install the LaunchAgent with the local virtualenv Python: ./.venv/bin/python spotter.py install-agent"
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
            str(root / "spotter.py"),
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


def install_launch_agent(config: dict[str, Any], config_path: Path, *, emit: bool = True) -> int:
    """Install or update the per-user macOS LaunchAgent."""
    app_log_dir(config).mkdir(parents=True, exist_ok=True)

    plist_path = launch_agent_plist_path(config)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_data = build_launch_agent_plist(config, config_path)
    atomic_write_plist(plist_path, plist_data)

    run_checked(["plutil", "-lint", str(plist_path)], emit=emit)
    bootout_launch_agent(config)
    run_checked(["launchctl", "bootstrap", launch_agent_domain(), str(plist_path)], emit=emit)

    if emit:
        print(f"Installed LaunchAgent {launch_agent_label(config)}.")
        print(f"Plist: {plist_path}")
        print(f"Logs: {app_log_dir(config)}")
    return 0


def uninstall_launch_agent(config: dict[str, Any], *, emit: bool = True) -> int:
    """Unload and remove the per-user macOS LaunchAgent."""
    plist_path = launch_agent_plist_path(config)
    bootout_launch_agent(config)

    if plist_path.exists():
        plist_path.unlink()
        if emit:
            print(f"Removed LaunchAgent plist: {plist_path}")
    else:
        if emit:
            print(f"LaunchAgent plist was already absent: {plist_path}")
    return 0


def show_launch_agent_status(config: dict[str, Any], config_path: Path) -> int:
    """Print the current launchd status for this scanner."""
    status = inspect_launch_agent(config, config_path)
    database_access = check_whatsapp_database_access(config)

    print(f"Label: {status.label}")
    print(f"Service: {status.service_name}")
    print(f"Plist: {status.plist_path}")
    print(f"Plist exists: {yes_no(status.plist_exists)}")
    print(f"Plist loadable: {yes_no(status.plist_loadable)}")
    print(f"Loaded: {yes_no(status.loaded)}")
    print(f"Matches current config: {status_label(status.plist_matches_config)}")
    print(f"Configured correctly: {yes_no(status.is_configured_correctly)}")
    print(f"Current Python: {status.current_python_path}")
    print(f"Installed Python: {status.installed_python_path or 'unknown'}")
    print(f"Installed config: {status.installed_config_path or 'unknown'}")
    print(f"App log: {status.app_log_path}")
    print(f"WhatsApp DB access: {yes_no(database_access.ok)}")
    print(f"WhatsApp DB: {database_access.db_path or 'unknown'}")
    print(f"WhatsApp access detail: {database_access.detail}")
    for detail in status_problem_details(status):
        print(f"Status detail: {detail}")
    return 0


def inspect_launch_agent(config: dict[str, Any], config_path: Path) -> LaunchAgentStatus:
    """Inspect the configured LaunchAgent without mutating launchd or the plist."""
    label = launch_agent_label(config)
    service_name = launch_agent_service_name(config)
    plist_path = launch_agent_plist_path(config)
    current_python_path = Path(sys.executable)
    if not current_python_path.is_absolute():
        current_python_path = (Path.cwd() / current_python_path).resolve()

    current_python_error = None
    expected_plist_error = None
    expected_plist = None
    try:
        expected_plist = build_launch_agent_plist(config, config_path)
    except (ConfigError, LaunchAgentError, OSError, ValueError) as exc:
        current_python_error = str(exc) if isinstance(exc, LaunchAgentError) else None
        expected_plist_error = str(exc)

    plist_data: dict[str, Any] | None = None
    plist_error = None
    if plist_path.exists():
        try:
            with plist_path.open("rb") as handle:
                loaded_plist = plistlib.load(handle)
            if isinstance(loaded_plist, dict):
                plist_data = loaded_plist
            else:
                plist_error = "Installed plist is not a dictionary."
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            plist_error = str(exc)

    plist_matches_config = None
    if plist_data is not None and expected_plist is not None:
        plist_matches_config = plist_data == expected_plist

    loaded, loaded_error = launch_agent_loaded(service_name)
    installed_python_path, installed_config_path = installed_program_paths(plist_data)

    return LaunchAgentStatus(
        label=label,
        service_name=service_name,
        plist_path=plist_path,
        app_log_path=app_log_path(config),
        current_python_path=current_python_path,
        current_python_error=current_python_error,
        plist_exists=plist_path.exists(),
        plist_loadable=plist_data is not None,
        plist_error=plist_error,
        loaded=loaded,
        loaded_error=loaded_error,
        plist_matches_config=plist_matches_config,
        expected_plist_error=expected_plist_error,
        installed_python_path=installed_python_path,
        installed_config_path=installed_config_path,
    )


def launch_agent_loaded(service_name: str) -> tuple[bool, str | None]:
    """Return whether launchd currently knows about the service."""
    try:
        result = subprocess.run(["launchctl", "print", service_name], capture_output=True, text=True, check=False)
    except OSError as exc:
        return False, str(exc)

    if result.returncode == 0:
        return True, None
    stderr = result.stderr.strip()
    if not stderr or "Could not find service" in stderr:
        return False, None
    return False, stderr


def run_checked(command: list[str], *, emit: bool) -> None:
    """Run a required subprocess, optionally suppressing output for TUI callers."""
    if emit:
        subprocess.run(command, check=True)
        return
    subprocess.run(command, capture_output=True, text=True, check=True)


def installed_program_paths(plist_data: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Return the Python and config paths from an installed plist."""
    if plist_data is None:
        return None, None

    arguments = plist_data.get("ProgramArguments")
    if not isinstance(arguments, list):
        return None, None

    python_path = str(arguments[0]) if arguments and isinstance(arguments[0], str) else None
    config_path = None
    for index, argument in enumerate(arguments):
        if argument == "--config" and index + 1 < len(arguments) and isinstance(arguments[index + 1], str):
            config_path = str(arguments[index + 1])
            break
    return python_path, config_path


def status_problem_details(status: LaunchAgentStatus) -> list[str]:
    """Return human-readable status details that need operator attention."""
    details = []
    if status.current_python_error:
        details.append(status.current_python_error)
    if status.expected_plist_error and status.expected_plist_error != status.current_python_error:
        details.append(status.expected_plist_error)
    if status.plist_error:
        details.append(f"Could not read plist: {status.plist_error}")
    if status.loaded_error:
        details.append(f"launchctl: {status.loaded_error}")
    if status.plist_exists and status.plist_matches_config is False:
        details.append("Installed plist differs from the current config; reinstall to update it.")
    return details


def yes_no(value: bool) -> str:
    """Format a boolean for CLI status output."""
    return "yes" if value else "no"


def status_label(value: bool | None) -> str:
    """Format a tri-state status for CLI status output."""
    if value is None:
        return "unknown"
    return yes_no(value)


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
