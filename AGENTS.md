# Project Overview

`spotter` is a local, single-user macOS CLI that watches WhatsApp group chats for messages matching topics the user defines in plain English, and pings them via macOS Notification Center and/or Pushover when something relevant comes through. The point is to keep noisy WhatsApp groups muted without missing the few high-signal messages that actually matter (job leads, deal mentions, a specific name dropped in a 200-person channel).

Operationally it reads the WhatsApp desktop app's `ChatStorage.sqlite` in read-only mode, pulls new group messages since a cursor stored in `state.json`, sends them in batches to OpenRouter's Chat Completions API with the configured topic descriptions, and writes validated matches to `alerts.jsonl` before firing notifications. The cursor only advances when every batch in a run succeeds, so transient API or rate-limit failures cause the next run to retry the same messages rather than silently drop them. A LaunchAgent runs the scan periodically; a Textual TUI shows run/alert/usage history.

Design constraints worth knowing before changing anything:
- The WhatsApp database is opened with SQLite URI `mode=ro` — never modify WhatsApp files.
- Single user, single machine, single LLM gateway (OpenRouter). No multi-tenancy or provider abstraction.
- Every WhatsApp message is treated as untrusted input; validated matches must reference a `message_pk` from the current batch and a `topic_id` from `config.json`.
- `README.md` has the full architecture, configuration model, and end-to-end scan flow if more context is needed.

# Project Instructions

- Always use the local Python virtual environment in `.venv` for this project.
- Run Python as `./.venv/bin/python` and install packages as `./.venv/bin/python -m pip ...`.
- Do not install or run dependencies against the global Python package repository.
- Load secrets from `.env`; never print or commit secret values.
- Open the WhatsApp database read-only only. Never modify WhatsApp files.
- Treat the TUI as a keyboard-first command-line interface. Do not add mouse-oriented controls such as clickable buttons for primary actions; expose actions through documented key bindings and make current status visible at a glance in text. Shortcuts for page-specific actions must only be active and visible on the relevant page.
- Before committing Python changes, run Ruff on the project with `./.venv/bin/python -m ruff format .` and `./.venv/bin/python -m ruff check .`.
- Run Ruff / formatting / linting only before committing
- Do not assume every change needs tests. Add tests only when they provide meaningful regression protection for behavior with real risk or complexity; skip low-leverage tests for straightforward wiring, configuration, and mechanical changes.
- Avoid including any information about real WhatsApp groups, their users and their messages' contents when writing test cases and evals
- Keep the Project Files section below in sync: whenever a tracked file is added, removed, renamed, or substantially repurposed, update its entry in the same change.

# Project Files

- `spotter.py` - Main CLI, scan orchestration, state writes, and error recording.
- `spotter/alerts.py` - Alert thresholding, topic-priority selection, deduplication, and formatting.
- `spotter/classifier.py` - Direct OpenRouter HTTP client: system prompt, JSON schema, batching, retry, parsing, and match validation.
- `spotter/config.py` - Central Pydantic-based typed configuration loading, defaults, and validation.
- `spotter/errors.py` - Shared application exception types.
- `spotter/identity.py` - Shared WhatsApp sender identity normalization and display fallbacks.
- `spotter/launchagent.py` - LaunchAgent generation, installation, removal, and status inspection.
- `spotter/models.py` - Shared domain dataclasses passed between scanner subsystems.
- `spotter/monitoring.py` - Dead Man's Snitch successful-run heartbeat delivery.
- `spotter/notifications.py` - macOS and Pushover notification delivery and formatting.
- `spotter/paths.py` - Runtime log and application path resolution.
- `spotter/preflight.py` - Read-only WhatsApp database access checks.
- `spotter/tui.py` - Keyboard-first Textual terminal interface.
- `spotter/usage.py` - Per-run LLM token and scanner usage records.
- `spotter/whatsapp_db.py` - Read-only WhatsApp SQLite queries and message conversion.
- `evals/README.md` - Manual live classifier eval workflow and case-authoring guidance.
- `evals/cases.jsonl` - Synthetic and scrubbed model-backed classifier regression cases.
- `evals/compare_models.py` - Multi-model classifier eval matrix runner with strict-then-freeform structured-output fallback.
- `evals/models.json` - Registry of OpenRouter slugs and per-model toggles consumed by `compare_models.py`.
- `evals/run_classifier_evals.py` - Manual live classifier eval runner and result reporting.
- `tests/support.py` - Shared unittest helpers and application logging suppression.
- `tests/test_alerts.py` - Alert deduplication and first-configured-topic regression test.
- `tests/test_classifier.py` - OpenRouter request, retry, usage, response validation, and typed match contract tests.
- `tests/test_config.py` - Typed configuration parsing, defaults, and validation tests.
- `tests/test_scan.py` - Happy-path scan test with mocked external services and validated temporary filesystem outputs.
- `tests/test_tui.py` - TUI navigation, sorting, refresh, configuration display, and redaction tests.
- `tests/test_usage.py` - Usage accumulator addition and merge behavior tests.
