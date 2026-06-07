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

## Compare Multiple Models

`evals/compare_models.py` runs the same eval suite across the slugs listed in `evals/models.json` and prints a comparison table.

```bash
./.venv/bin/python evals/compare_models.py --live
```

For each model the driver runs every case sequentially, then prints a per-model summary with post-threshold pass count, raw (pre-threshold) match correctness, p50 latency, and aggregate token usage. A JSON artifact with full per-case detail is written to `evals/results/compare_<utc-timestamp>.json` (gitignored). The slugs in `evals/models.json` are expected to exist on OpenRouter at authoring time; the driver does not pre-flight the catalog.

If a provider rejects the strict `response_format` JSON schema, the runner falls back to freeform JSON (system-prompt only) for the rest of that model and marks its mode as `freeform` in the output. Per-model overrides in `evals/models.json` can also force `omit_temperature`, pre-disable structured output, or set `"skip": true` to keep a slug in the registry without running it.

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
