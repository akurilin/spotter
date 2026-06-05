# Project Instructions

- Always use the local Python virtual environment in `.venv` for this project.
- Run Python as `./.venv/bin/python` and install packages as `./.venv/bin/python -m pip ...`.
- Do not install or run dependencies against the global Python package repository.
- Load secrets from `.env`; never print or commit secret values.
- Open the WhatsApp database read-only only. Never modify WhatsApp files.
- Treat the TUI as a keyboard-first command-line interface. Do not add mouse-oriented controls such as clickable buttons for primary actions; expose actions through documented key bindings and make current status visible at a glance in text. Shortcuts for page-specific actions must only be active and visible on the relevant page.
- Before committing Python changes, run Ruff on the project with `./.venv/bin/python -m ruff format .` and `./.venv/bin/python -m ruff check .`.
- Run Ruff / formatting / linting only before committing

# Project Files

- `spotter.py` - Main CLI, scan orchestration, LLM classification, alert construction, and state writes.
- `spotter/errors.py` - Shared application exception types.
- `spotter/launchagent.py` - LaunchAgent generation, installation, removal, and status inspection.
- `spotter/notifications.py` - macOS and Pushover notification delivery and formatting.
- `spotter/paths.py` - Runtime log and application path resolution.
- `spotter/preflight.py` - Read-only WhatsApp database access checks.
- `spotter/tui.py` - Keyboard-first Textual terminal interface.
- `spotter/usage.py` - Per-run LLM token and scanner usage records.
- `spotter/whatsapp_db.py` - Read-only WhatsApp SQLite queries and message conversion.
- `tests/support.py` - Shared unittest helpers and application logging suppression.
- `tests/test_alerts.py` - Alert deduplication and first-configured-topic regression test.
- `tests/test_scan.py` - Happy-path scan test with mocked external services and validated temporary filesystem outputs.
