# WhatsApp Topic Alerts

Local WhatsApp group scanner for topic-based alerts. It reads the macOS WhatsApp SQLite database in read-only mode, sends batches of new group messages to Claude, writes matches to `alerts.jsonl`, and optionally sends a macOS notification.

## Setup

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
```

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
./.venv/bin/python wap_alerts.py run
```

Dry-run without writing state, alerts, or notifications:

```bash
./.venv/bin/python wap_alerts.py run --dry-run
```

Dry-run against a smaller number of messages:

```bash
./.venv/bin/python wap_alerts.py run --dry-run --limit 100
```

Test enabled notifications, including macOS and Pushover:

```bash
./.venv/bin/python wap_alerts.py test-notification
```

## State

The scanner uses one universal WhatsApp message cursor in `state.json`. On the first run, when no cursor exists, it scans the last 14 days of group messages.

Claude classification is all-or-nothing per run. If any batch fails because of timeouts, rate limits, unavailable API, billing issues, or malformed output, the cursor is not advanced.

## Safety

The WhatsApp database is opened with SQLite URI `mode=ro`. This project only writes local app-owned files such as `state.json`, `alerts.jsonl`, and `errors.jsonl`.
