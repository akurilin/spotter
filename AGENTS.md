# Project Instructions

- Always use the local Python virtual environment in `.venv` for this project.
- Run Python as `./.venv/bin/python` and install packages as `./.venv/bin/python -m pip ...`.
- Do not install or run dependencies against the global Python package repository.
- Load secrets from `.env`; never print or commit secret values.
- Open the WhatsApp database read-only only. Never modify WhatsApp files.
- Treat the TUI as a keyboard-first command-line interface. Do not add mouse-oriented controls such as clickable buttons for primary actions; expose actions through documented key bindings and make current status visible at a glance in text. Shortcuts for page-specific actions must only be active and visible on the relevant page.
- Before committing Python changes, run Ruff on the project with `./.venv/bin/python -m ruff format .` and `./.venv/bin/python -m ruff check .`.
- Ruff does not need to run after every small edit; run it at commit/checkpoint time or when formatting/lint feedback is specifically useful.
