# spotter

Local WhatsApp group scanner for topic-based alerts. It reads the macOS WhatsApp SQLite database in read-only mode, sends batches of new group messages to Claude, writes matches to `alerts.jsonl`, and optionally sends a macOS notification.

## Requirements

- **macOS only.** The scanner reads the WhatsApp desktop database at `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`, sends notifications through `osascript`, and runs on a schedule via launchd. None of these are portable to Linux or Windows.
- **Anthropic API only.** Classification is hard-coded to the Anthropic SDK; there is no provider abstraction.
- **Python 3.12+.** The code uses `from datetime import UTC` and PEP 604 union syntax.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

For development tooling:

```bash
./.venv/bin/python -m pip install -r requirements-dev.txt
```

Copy the example configuration and edit the topics you want to be alerted about:

```bash
cp config.example.json config.json
```

`config.json` is gitignored, so your topics, LaunchAgent label, and other personal settings stay local.

The Anthropic API key is loaded from `.env` as `ANTHROPIC_API_KEY`.

Pushover notifications are enabled by default. Add these to `.env`:

```bash
PUSHOVER_APP_TOKEN=...
PUSHOVER_USER_KEY=...
```

The Pushover application icon used for this notifier is tracked at `assets/icon.png`. Pushover shows the icon configured on the Pushover application, so this asset is kept here as the source image for that app setting.

## Commands

Run the scanner:

```bash
./.venv/bin/python spotter.py run
```

Dry-run without writing state, alerts, or notifications:

```bash
./.venv/bin/python spotter.py run --dry-run
```

Dry-run against a smaller number of messages:

```bash
./.venv/bin/python spotter.py run --dry-run --limit 100
```

Test enabled notifications, including macOS and Pushover:

```bash
./.venv/bin/python spotter.py test-notification
```

Install the macOS LaunchAgent:

```bash
./.venv/bin/python spotter.py install-agent
```

The LaunchAgent schedule and whether it runs immediately on install are controlled by the `launch_agent` section in `config.json`.

Check LaunchAgent status:

```bash
./.venv/bin/python spotter.py agent-status
```

Tail the scanner log:

```bash
tail -f ~/Library/Logs/spotter/spotter.log
```

Uninstall the LaunchAgent:

```bash
./.venv/bin/python spotter.py uninstall-agent
```

Format Python code:

```bash
./.venv/bin/python -m ruff format .
```

Run Python lint and best-practice checks:

```bash
./.venv/bin/python -m ruff check .
```

Apply safe lint fixes:

```bash
./.venv/bin/python -m ruff check --fix .
```

## State

The scanner uses one universal WhatsApp message cursor in `state.json`. On the first run, when no cursor exists, it scans the last 14 days of group messages.

Claude classification is all-or-nothing per run. If any batch fails because of timeouts, rate limits, unavailable API, billing issues, or malformed output, the cursor is not advanced.

## Safety

The WhatsApp database is opened with SQLite URI `mode=ro`. This project only writes local app-owned files such as `state.json`, `alerts.jsonl`, and `errors.jsonl`.

Message bodies are passed to Claude as untrusted data, so a crafted WhatsApp message could attempt prompt injection. The blast radius is small: validated matches must reference a `message_pk` from the current batch and a `topic_id` from your config, `osascript` and Pushover receive notification text as arguments rather than interpolated script, and there is no shell, `eval`, or template rendering anywhere in the pipeline. The realistic worst case is misleading or spammy alert text on your screen, not code execution.

## Logging

The default log directory is `~/Library/Logs/spotter`, which matches the usual macOS convention for per-user application logs. The path and log level are controlled by the `logging` section in `config.json`.

The scanner logs operational progress such as cursor state, group/message counts, Claude model, batch progress, match counts, alert counts, and notification backend activity. Routine logs avoid message bodies; dry-run alert output still prints matching messages to the terminal for review.

Notification delivery is best-effort. If macOS or Pushover delivery fails, the scanner logs the failure and writes a structured `notification_failed` entry to `errors.jsonl` without blocking cursor advancement. Notification text includes the resolved sender name and the local time the alert was created.

## LaunchAgent

The `install-agent` command writes a generated plist to `~/Library/LaunchAgents/<launch_agent.label>.plist` using the label configured in `config.json` (default `com.example.spotter`).
It pins the background job to the same Python virtualenv used for installation by recording `sys.executable` in the plist.

The generated LaunchAgent uses this project directory as `WorkingDirectory`, records the configured interval and run-at-load behavior from `config.json`, and writes stdout/stderr logs to the configured log directory.

Do not install it with global Python. Use:

```bash
./.venv/bin/python spotter.py install-agent
```

# WIP / TODO

- Fault tolerance / resumability of initial parsing to save on tokens
- Not overwhelm users with the initial matches if there are many of them
