# Manual Classifier Evals

This directory stores model-backed eval cases for classifier regressions. These are not unit tests and should not run in CI: they call the configured model through OpenRouter and spend real tokens.

## Run

List cases without calling the model:

```bash
./.venv/bin/python evals/run_classifier_evals.py --list
```

Run all cases against the live model and current `config.json`:

```bash
./.venv/bin/python evals/run_classifier_evals.py --live --verbose
```

Run one case:

```bash
./.venv/bin/python evals/run_classifier_evals.py --live --case robotics_nyc_openclaw_ai_agent_workshop_false_positive
```

Compare another model without editing `config.json`:

```bash
./.venv/bin/python evals/run_classifier_evals.py --live --model anthropic/claude-opus-4.6 --omit-temperature
```

The runner loads `.env`, uses the model and topic definitions from `config.json`, calls the same `classify_messages` path as production, applies topic thresholds through `build_alerts`, and reports pass/fail against the expected alert behavior. A live run also fails when OpenRouter does not return positive input and output token counts, which validates the production usage-field mapping.

Manual evals require `llm.temperature` to be `0` for maximum reproducibility. Some models reject the `temperature` parameter; use `--omit-temperature` or set `llm.temperature` to JSON `null` for those models. To compare models, use an OpenRouter model ID with `--model` or pass a separate config with `--config`. Secrets are loaded from the repo `.env` by default; pass `--env` if you need a different file.

## Add Cases

Add one JSON object per line to `cases.jsonl`.

Required fields:

- `id`: stable snake_case identifier.
- `name`: short human-readable name.
- `messages`: one or more synthetic or scrubbed WhatsApp-like messages.
- `expected_matches`: message/topic pairs that should alert after thresholds.
- `expected_non_matches`: message/topic pairs that must not alert after thresholds.
- `allow_extra_matches`: set to `false` for targeted regression cases unless extra alerts are intentionally acceptable.

Optional fields:

- `failure_type`: for example `false_positive` or `false_negative`.
- `notes`: brief context about the historical failure.

Before committing a case, scrub real people, private group names, real group JIDs, real sender JIDs, and private event links. Keep only the minimum text needed to reproduce the classifier behavior.
