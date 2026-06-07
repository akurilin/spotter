# spotter

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![macOS](https://img.shields.io/badge/macOS-14%2B-000000?logo=apple&logoColor=white)](https://www.apple.com/macos/)
[![OpenRouter API](https://img.shields.io/badge/OpenRouter-API-6467F2)](https://openrouter.ai/docs)
[![lint: ruff](https://img.shields.io/badge/lint-ruff-D7FF64.svg)](https://docs.astral.sh/ruff/)
[![Tests](https://github.com/akurilin/spotter/actions/workflows/test.yml/badge.svg)](https://github.com/akurilin/spotter/actions/workflows/test.yml)

`spotter` is a local, single-user WhatsApp group scanner that pings you only when a message matches a topic you actually care about. It reads the macOS WhatsApp desktop SQLite database in read-only mode, sends batches of new group messages through OpenRouter for natural-language classification, writes matches to `alerts.jsonl`, and delivers notifications via macOS Notification Center and/or [Pushover](https://pushover.net/) for push to your phone.

The problem it solves: WhatsApp groups produce a tide of low-signal chatter, so most people mute them and then miss the few messages that actually matter — a job lead, a deal, a specific name dropped in a 200-person channel. Keyword filters don't survive contact with how people actually phrase things. spotter keeps WhatsApp muted but routes the messages you care about back into your day, with the classification described in plain English in `config.json` and the WhatsApp database never leaving your laptop.

## Highlights

Features spotter is particularly proud of:

- **Natural-language topics.** You describe what you want to be alerted about in plain English in `config.json` — including the near-misses you want excluded — and the configured model does the classification. No regexes, no keyword lists, no per-group rules.
- **Local-only WhatsApp access.** Reads the WhatsApp desktop database directly via SQLite `mode=ro`. No web scraping or Business API. The bytes never leave your machine except the message text you explicitly send through OpenRouter for classification.
- **All-or-nothing cursor advance.** A universal cursor in `state.json` tracks the last scanned message. If any batch fails because of timeouts, rate limits, or malformed model output, the cursor stays put and the next run retries from the same position — no silently-dropped messages, no per-topic state to keep in sync.
- **Independent notification backends.** macOS Notification Center and Pushover can be toggled separately in `config.json`. Good when you want banners at the laptop and pushes on your phone — or only one of those — without forking config.
- **Prompt-injection-aware design.** Every WhatsApp message is treated as untrusted input. Validated matches must reference a `message_pk` from the current batch and a `topic_id` from your config, notification payloads are passed as arguments rather than interpolated into scripts, and there is no shell, `eval`, or template rendering in the path. The realistic worst case is spammy alert text, not code execution.
- **Per-run usage logging.** Every scan appends a structured token-usage record to `usage.jsonl` so you can audit model usage.
- **Dead Man's Snitch monitoring.** Successful scheduled scans can ping a secret check-in URL so missing or failed runs trigger an external alert.
- **LaunchAgent-managed.** `install-agent` writes a generated plist pinned to the project's `.venv` Python, with interval and run-at-load behavior driven by `config.json`. `agent-status` and `uninstall-agent` round out the lifecycle.
- **Dry-run modes.** `--dry-run` and `--dry-run --limit N` let you exercise the full pipeline against real WhatsApp data without writing state, alerts, or notifications. Matches still print to the terminal so you can sanity-check topic descriptions before committing.
- **Deliberately narrow scope.** Single user, single machine, single LLM gateway, single notification flow. No multi-tenancy, no provider abstraction, no plugin system.

## Architecture

spotter is a single-process Python CLI split into a thin entry point plus a small package of helpers:

| Component                       | Role                                                                                  |
| ------------------------------- | ------------------------------------------------------------------------------------- |
| `spotter.py`                    | CLI entry point, scan orchestration, state writes, and error recording                |
| `spotter/alerts.py`             | Alert thresholding, topic-priority selection, deduplication, and formatting           |
| `spotter/classifier.py`         | Direct OpenRouter HTTP calls, batching, retries, response parsing, and validation      |
| `spotter/config.py`             | Pydantic-based typed configuration loading, defaults, and validation                  |
| `spotter/identity.py`           | Shared sender identity normalization and display fallbacks                            |
| `spotter/models.py`             | Shared domain values passed between scanner subsystems                                |
| `spotter/whatsapp_db.py`        | Read-only access to the WhatsApp `ChatStorage.sqlite`, group filtering, cursor reads  |
| `spotter/notifications.py`      | macOS Notification Center (`osascript`) and Pushover HTTP delivery                    |
| `spotter/monitoring.py`         | Dead Man's Snitch successful-run heartbeat delivery                                  |
| `spotter/launchagent.py`        | Generate, install, query, and remove the LaunchAgent plist                            |
| `spotter/tui.py`                | Textual terminal UI for run history, alert history, and LaunchAgent controls          |
| `spotter/usage.py`              | Per-run model token-usage records appended to `usage.jsonl`                           |
| `spotter/errors.py`             | Structured error records for `errors.jsonl`                                           |
| `spotter/paths.py`              | Runtime log path resolution                                                           |

Runtime targets:

- **Classifier:** OpenRouter Chat Completions API (default `anthropic/claude-sonnet-4.6`, configurable in `config.json`).
- **State + outputs:** plain JSON / JSONL files under `~/Library/Application Support/spotter/` and `~/Library/Logs/spotter/`.
- **Scheduler:** macOS launchd via a generated user-level LaunchAgent.
- **Notifications:** macOS Notification Center via `osascript`, Pushover via plain HTTPS.

### How a scan flows end to end

1. The CLI loads `config.json` and `.env`, configures logging, and opens the WhatsApp `ChatStorage.sqlite` read-only.
2. It reads the universal cursor from `state.json`. On the first run with no cursor, it backfills the last `initial_backfill_days` of group messages (default 14).
3. New group messages since the cursor are pulled and stripped of system messages, status updates, and (by default) the user's own messages.
4. Messages are batched (default 100 per batch) and each batch is sent through OpenRouter with the topic descriptions as a system prompt and a JSON-schema-constrained matches response.
5. Each response is validated: every match must reference a `message_pk` present in the batch and a `topic_id` defined in `config.json`. Malformed batches abort the run without advancing the cursor.
6. Validated matches are composed into alerts, deduped against the existing `alerts.jsonl`, and written to disk.
7. Notifications fire through every enabled backend. Failures are logged to `errors.jsonl` as `notification_failed` entries but do not block cursor advance.
8. The cursor is advanced and a token-usage record is appended to `usage.jsonl` — **only** if every batch in the run classified successfully.
9. A successful non-dry-run scan sends a GET request to `DEAD_MANS_SNITCH_URL` when configured. Failed scans deliberately do not check in.

WhatsApp database files are never modified. Configured service credentials are sent only to their corresponding services.

## Requirements

### Must-haves

- **macOS 14 or later** with the [WhatsApp desktop app](https://www.whatsapp.com/download) installed and signed in — that's what populates the `ChatStorage.sqlite` database spotter reads. iOS / iPad / web won't work.
- **Python 3.12+** — the code uses `from datetime import UTC` and PEP 604 union syntax.
- **An [OpenRouter API key](https://openrouter.ai/settings/keys)** — classification calls OpenRouter directly with Python's standard HTTP library; there is no LLM SDK or provider abstraction. Usage is per-batch, capped by `max_messages_per_run`, and recorded in `usage.jsonl`.
- **Full Disk Access for the Python binary that runs spotter** — macOS gates the WhatsApp Group Container behind TCC, so without this grant the read-only `sqlite3.connect` will fail with "unable to open database file" even though the path is correct and your user owns it. Add `.venv/bin/python3` (or whatever interpreter you use, including the one launchd invokes) under **System Settings → Privacy & Security → Full Disk Access**. spotter opens the file with SQLite URI `mode=ro` and never writes to it.

### Nice-to-haves

- **A [Pushover](https://pushover.net) account, app token, and user key** — only needed if you want alerts pushed to your phone when you're away from the laptop. Free trial works fine for testing; the one-time license is cheap.
- **A [Dead Man's Snitch](https://deadmanssnitch.com/) check-in URL** — only needed if you want an external alert when scheduled scans stop succeeding.
- **Ruff** — installed via `requirements-dev.txt` for `ruff format` and `ruff check` before commits.

## Configuration model

spotter has two configuration files, both gitignored, both bootstrapped from `.example` templates:

- **`config.json`** holds everything about *behavior*: topics, WhatsApp DB path, batch sizes, LLM model + token caps, notification toggles, LaunchAgent label and interval, log directory, and output file paths. Copy it from `config.example.json` and edit the topics.
- **`.env`** holds *secrets only*: `OPENROUTER_API_KEY` (required), optional Pushover credentials, and the optional `DEAD_MANS_SNITCH_URL` check-in credential. Copy it from `.env.example`.

This split lets you check in changes to `config.example.json` (defaults, new options) without touching anyone's local secrets, and lets you rotate keys without rewriting your topic config.

The Pushover application icon used for this notifier is tracked at `assets/icon.png`. Pushover shows whatever icon is configured on the Pushover *application* server-side, so this file is kept as the source image for that setting — not loaded by the code at runtime.

## Getting started

First-time setup from a clean checkout.

```bash
# 1. Create the virtualenv and install runtime dependencies.
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt

# 2. (Optional) install development tooling — Ruff for format + lint.
./.venv/bin/python -m pip install -r requirements-dev.txt

# 3. Bootstrap both configuration files from their templates.
cp config.example.json config.json
cp .env.example .env
```

Now work through the blanks:

1. **OpenRouter key.** Mint a key at [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys) and paste it into `OPENROUTER_API_KEY` in `.env`.
2. **Topics.** Edit the `topics` array in `config.json`. Each entry is `{ id, name, description, threshold }`. The `description` is what the classifier sees — be explicit about both the intent you care about *and* the near-misses you want excluded. Precision in the description directly reduces false positives.
3. **Grant Full Disk Access to the virtualenv Python.** macOS will otherwise block the read of `ChatStorage.sqlite` with "unable to open database file". Open **System Settings → Privacy & Security → Full Disk Access**, click **+**, press <kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>.</kbd> to reveal hidden files, navigate to this repo's `.venv/bin/`, and add `python3` (the symlink resolves to the real interpreter). If you later install the LaunchAgent, the same binary is what launchd runs, so this single grant covers both interactive and scheduled runs.
4. **Pushover (optional).** If you want phone pushes, create a Pushover account + application, then paste `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` into `.env` and leave `notifications.pushover` set to `true` in `config.json`. Otherwise flip it to `false`.
5. **Dead Man's Snitch (optional).** Create a snitch with a deadline longer than `launch_agent.start_interval_seconds`, then paste its secret check-in URL into `DEAD_MANS_SNITCH_URL` in `.env`.
6. **Smoke test.** Run a dry run against a small slice of messages to validate your topic descriptions:
   ```bash
   ./.venv/bin/python spotter.py run --dry-run --limit 100
   ```
   Matches will print to the terminal without writing state, alerts, or notifications.
7. **First real run.** Drop `--dry-run` to advance the cursor, fire notifications, and check in with Dead Man's Snitch when configured:
   ```bash
   ./.venv/bin/python spotter.py run
   ```
8. **Install the LaunchAgent.** Once you trust the topic config, schedule it:
   ```bash
   ./.venv/bin/python spotter.py install-agent
   ```
   The interval and run-at-load behavior come from `config.json → launch_agent`.

## Commands

All commands run through the project's virtualenv Python.

```bash
# Run a full scan.
./.venv/bin/python spotter.py run

# Dry-run — no state, alerts, or notifications written. Matches print to stdout.
./.venv/bin/python spotter.py run --dry-run

# Dry-run capped at N messages, regardless of `max_messages_per_run` in config.
./.venv/bin/python spotter.py run --dry-run --limit 100

# Fire a test notification through every enabled backend.
./.venv/bin/python spotter.py test-notification

# Open the terminal UI for run history, alert history, and LaunchAgent controls.
# Inside the TUI, press 1 for Runs, 2 for Alerts, 3 for Agent, e to enable scheduled runs,
# d to disable scheduled runs, F5 to refresh, and q to quit.
./.venv/bin/python spotter.py tui

# Install / inspect / remove the macOS LaunchAgent.
./.venv/bin/python spotter.py install-agent
./.venv/bin/python spotter.py agent-status
./.venv/bin/python spotter.py uninstall-agent

# Tail the scanner log.
tail -f ~/Library/Logs/spotter/spotter.log

# Format and lint Python code.
./.venv/bin/python -m ruff format .
./.venv/bin/python -m ruff check .
./.venv/bin/python -m ruff check --fix .
```

## Manual classifier evals

A small set of scrubbed historical failures and synthetic stratified cases (obvious positives, obvious negatives, borderline phrasings, mixed-message batches) lives in `evals/cases.jsonl` and can be replayed by hand against the configured live model. They are deliberately separate from normal tests because they call OpenRouter and spend real tokens.

```bash
# Inspect cases without calling the model.
./.venv/bin/python evals/run_classifier_evals.py --list

# Run the suite manually against the current config/model.
./.venv/bin/python evals/run_classifier_evals.py --live --verbose

# Compare another model without editing config.json.
./.venv/bin/python evals/run_classifier_evals.py --live --model anthropic/claude-opus-4.6 --omit-temperature
```

See `evals/README.md` for the case format and privacy-scrubbing expectations.
The eval runner uses `llm.model` from `config.json` and refuses to run unless `llm.temperature` is `0` or JSON `null` for models that reject the temperature parameter.

### Comparing multiple models

`evals/compare_models.py` runs the same case suite across the OpenRouter slugs declared in `evals/models.json` and prints a comparison table (pass / raw pre-threshold correctness / p50 latency / token usage / error count) per model. A JSON artifact with full per-case detail is written to `evals/results/compare_<utc-timestamp>.json` (gitignored).

```bash
./.venv/bin/python evals/compare_models.py --live
```

Each registry entry is a slug plus optional toggles: `"omit_temperature": true` for providers that reject the temperature parameter, `"use_structured_output": false` to pre-disable strict JSON schema mode, and `"skip": true` to keep a model in the registry without running it (useful for parking expensive baselines like Sonnet/Opus that aren't part of every sweep). If a provider rejects the strict `response_format` JSON schema mid-run, the driver falls back to freeform JSON for the rest of that model and marks its mode as `freeform` in the output.

## State

The scanner uses one universal WhatsApp message cursor in `state.json`. On the first run, when no cursor exists, it scans the last `initial_backfill_days` of group messages (default 14).

Classification is all-or-nothing per run. If any batch fails because of timeouts, rate limits, unavailable API, billing issues, or malformed output, the cursor is not advanced and the next run reprocesses the same messages.

## Safety

The WhatsApp database is opened with SQLite URI `mode=ro`. spotter only writes local app-owned files: `state.json`, `alerts.jsonl`, `errors.jsonl`, `usage.jsonl`, and `spotter.log`.

Message bodies are passed to the configured model as untrusted data, so a crafted WhatsApp message could attempt prompt injection. The blast radius is small: validated matches must reference a `message_pk` from the current batch and a `topic_id` from your config, `osascript` and Pushover receive notification text as arguments rather than as interpolated script, and there is no shell, `eval`, or template rendering anywhere in the pipeline. The realistic worst case is misleading or spammy alert text on your screen, not code execution.

## Notifications

Two backends can be enabled independently from the `notifications` section of `config.json`:

- **macOS Notification Center** (`notifications.macos`, default on) posts a banner via `osascript`. Good when you're at your laptop.
- **Pushover** (`notifications.pushover`) pushes to phones, tablets, and the Pushover desktop apps. Useful when you want WhatsApp topic hits to follow you off the laptop without leaving WhatsApp itself enabled for noisy group notifications. Requires `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` in `.env`, plus a free or paid Pushover account.

Optional Pushover knobs (`pushover_device`, `pushover_priority`, `pushover_sound_name`, `pushover_url`, `pushover_url_title`) can also be set in `notifications` and are forwarded to the Pushover API.

Notification delivery is best-effort. If macOS or Pushover delivery fails, the scanner logs the failure and writes a structured `notification_failed` entry to `errors.jsonl` without blocking cursor advancement. Notification text includes the resolved sender name and the local time the alert was created.

## Monitoring

Set the optional secret `DEAD_MANS_SNITCH_URL` in `.env` to monitor scanner health. After every successful non-dry-run scan, including scans with no new messages, spotter sends one HTTPS GET request to that URL. Failed scans and dry runs do not check in, allowing Dead Man's Snitch to alert when the LaunchAgent stops running or scans repeatedly fail.

Heartbeat delivery is best-effort and happens after scan state and usage records are written. A failed heartbeat is logged and written to `errors.jsonl` as `dead_mans_snitch_failed` without changing cursor state or the scan exit code. The secret URL is never written to logs or error records.

## Logging

The default log directory is `~/Library/Logs/spotter`, which matches the usual macOS convention for per-user application logs. The directory and log level are controlled by the `logging` section in `config.json`; the file paths for `state`, `alerts`, `errors`, and `usage` are independently configurable under `files`.

The scanner logs operational progress such as cursor state, group/message counts, model, batch progress, match counts, alert counts, and notification backend activity. Routine logs avoid message bodies; dry-run alert output still prints matching messages to the terminal for review.

Every artifact spotter writes — read this table first when debugging:

| File                                                          | Purpose                                                                                                                                          | When to read it                                                                                  |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| `~/Library/Logs/spotter/spotter.log`                          | Main scanner log: config load, cursor state, group/message counts, model, batch progress, match counts, notification backend activity.           | Default "what happened on the last run".                                                         |
| `~/Library/Logs/spotter/alerts.jsonl`                         | Append-only history of every alert spotter has surfaced, one JSON object per line. Used for dedupe across runs.                                  | Auditing past matches; sanity-checking that an expected alert actually fired.                    |
| `~/Library/Logs/spotter/errors.jsonl`                         | Structured error records (`notification_failed`, `dead_mans_snitch_failed`, classifier failures), one JSON object per line.                      | Finding what failed silently — best-effort delivery failures don't block cursor advance.         |
| `~/Library/Logs/spotter/usage.jsonl`                          | One JSON line per successful run with model input/output token usage and model name.                                                             | Auditing API spend; spotting runs that classified more batches than expected.                    |
| `~/Library/Logs/spotter/launchd.err.log`                     | stderr captured by launchd for scheduled runs (`StandardErrorPath` in the generated plist).                                                      | Scheduled runs that don't even reach `spotter.log` — Python startup errors, import errors, or missing config. |
| `~/Library/Application Support/spotter/state.json`            | The universal scan cursor (last processed WhatsApp message). Atomic, written only after every batch in a run succeeds.                           | Manually resetting or backfilling. Delete the file to re-trigger the initial `initial_backfill_days` backfill on the next run. |

When an expected alert never arrived, the canonical sequence is: check `spotter.log` for the run, then `errors.jsonl` for a `notification_failed` record, then (for scheduled runs) `launchd.err.log` if the run never logged anything at all.

## LaunchAgent

The `install-agent` command writes a generated plist to `~/Library/LaunchAgents/<launch_agent.label>.plist` using the label configured in `config.json` (default `com.example.spotter`). It pins the background job to the same Python virtualenv used for installation by recording `sys.executable` in the plist.

The generated LaunchAgent uses this project directory as `WorkingDirectory`, records the configured interval and run-at-load behavior from `config.json`, and writes stderr to `launchd.err.log` in the configured log directory. Standard output is not captured because scanner logs are already written to `spotter.log`.

The `agent-status` command and the TUI's **3 Agent** tab report whether the plist exists, whether launchd has loaded it, whether it still matches the current config, which Python/config paths are installed, and whether the current Python process can open the WhatsApp database read-only. A database access failure usually means Full Disk Access has not been granted to the local virtualenv Python.

Do not install it with global Python. Use:

```bash
./.venv/bin/python spotter.py install-agent
```

## WIP / TODO

- Fault tolerance / resumability of initial parsing to save on tokens.
- Avoid overwhelming users with the initial matches if there are many of them on the first run.
