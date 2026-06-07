#!/usr/bin/env python3
"""Run the classifier eval suite across multiple OpenRouter models and report comparison stats."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = Path(__file__).with_name("cases.jsonl")
DEFAULT_MODELS_PATH = Path(__file__).with_name("models.json")
DEFAULT_RESULTS_DIR = Path(__file__).with_name("results")

# This file is run directly, so the repository root is not otherwise importable.
sys.path.insert(0, str(REPO_ROOT))

from evals.run_classifier_evals import (  # noqa: E402
    EvalCase,
    evaluate_case,
    load_cases,
    validate_case_topics,
)
from spotter.alerts import build_alerts  # noqa: E402
from spotter.classifier import classify_messages  # noqa: E402
from spotter.config import AppConfig, load_config, load_env_file  # noqa: E402
from spotter.errors import ClassificationError  # noqa: E402


@dataclass(frozen=True)
class ModelEntry:
    slug: str
    omit_temperature: bool = False
    use_structured_output: bool = True
    skip: bool = False


@dataclass(frozen=True)
class CaseExecution:
    case_id: str
    passed: bool
    raw_passed: bool
    failures: tuple[str, ...]
    raw_failures: tuple[str, ...]
    latency_ms: float
    input_tokens: int
    output_tokens: int
    raw_match_count: int
    alert_count: int
    error: str | None


@dataclass
class ModelRunSummary:
    slug: str
    structured_mode: str
    executions: list[CaseExecution] = field(default_factory=list)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run classifier evals across multiple OpenRouter models and print a comparison table."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH, help="Path to JSONL eval cases.")
    parser.add_argument("--models", type=Path, default=DEFAULT_MODELS_PATH, help="Path to JSON model registry.")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.json", help="Path to spotter config JSON.")
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env", help="Path to the .env file with secrets.")
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help="Directory where per-run JSON artifacts are written.",
    )
    parser.add_argument("--case", action="append", dest="case_ids", help="Run only this case id. Can be repeated.")
    parser.add_argument("--live", action="store_true", help="Actually call the configured models.")
    parser.add_argument("--allow-ci", action="store_true", help="Permit live evals when CI is set.")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-case failure detail after the summary table.",
    )
    args = parser.parse_args()

    cases = load_cases(args.cases.expanduser().resolve())
    if args.case_ids:
        requested = set(args.case_ids)
        cases = [case for case in cases if case.id in requested]
        missing = requested - {case.id for case in cases}
        if missing:
            print(f"Unknown case id(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    entries = load_model_entries(args.models.expanduser().resolve())

    if not args.live:
        active = sum(1 for entry in entries if not entry.skip)
        skipped = len(entries) - active
        print(f"Loaded {len(cases)} case(s) and {len(entries)} model(s) ({active} active, {skipped} skipped).")
        print("Refusing to call live models without --live.")
        return 2

    if os.environ.get("CI") and not args.allow_ci:
        print("Refusing to run live evals in CI without --allow-ci.", file=sys.stderr)
        return 2

    config_path = args.config.expanduser().resolve()
    load_env_file(args.env.expanduser().resolve())
    config = load_config(config_path)
    if config.llm.temperature not in (0, None):
        print(
            "Refusing to run with non-zero llm.temperature. "
            f"Set llm.temperature to 0 or null in {config_path} for reproducible manual evals.",
            file=sys.stderr,
        )
        return 2
    validate_case_topics(cases, {topic.id for topic in config.topics})

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set. Add it to .env.", file=sys.stderr)
        return 2

    summaries: list[ModelRunSummary] = []
    for entry in entries:
        if entry.skip:
            print(f"[skip] {entry.slug}")
            continue
        print(f"[run] {entry.slug}")
        summary = run_model(config, cases, entry)
        summaries.append(summary)
        print_model_progress(summary)

    print_comparison_table(summaries, len(cases))
    if args.verbose:
        print_failure_details(summaries)

    artifact_path = write_artifact(args.results_dir.expanduser().resolve(), summaries, cases, config)
    print(f"\nDetailed artifact: {artifact_path}")
    return 0


def load_model_entries(path: Path) -> list[ModelEntry]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} must be a non-empty JSON array.")
    entries = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            raise ValueError(f"Invalid model entry in {path}: {item!r}")
        entries.append(
            ModelEntry(
                slug=item["slug"].strip(),
                omit_temperature=bool(item.get("omit_temperature", False)),
                use_structured_output=bool(item.get("use_structured_output", True)),
                skip=bool(item.get("skip", False)),
            )
        )
    return entries


def run_model(config: AppConfig, cases: list[EvalCase], entry: ModelEntry) -> ModelRunSummary:
    """Run every case for one model, falling back to freeform JSON if the provider rejects strict schema mode."""
    cfg = apply_entry_overrides(config, entry)
    summary = ModelRunSummary(
        slug=entry.slug,
        structured_mode="strict" if cfg.llm.use_structured_output else "freeform",
    )

    for case in cases:
        execution = run_one_case(cfg, case)
        if execution.error and cfg.llm.use_structured_output and looks_like_schema_error(execution.error):
            cfg = cfg.model_copy(update={"llm": cfg.llm.model_copy(update={"use_structured_output": False})})
            summary.structured_mode = "freeform"
            print(f"  [fallback] {entry.slug}: disabling structured output and retrying this case.")
            execution = run_one_case(cfg, case)
        summary.executions.append(execution)

    return summary


def apply_entry_overrides(config: AppConfig, entry: ModelEntry) -> AppConfig:
    llm_updates: dict[str, Any] = {
        "model": entry.slug,
        "use_structured_output": entry.use_structured_output,
    }
    if entry.omit_temperature:
        llm_updates["temperature"] = None
    return config.model_copy(update={"llm": config.llm.model_copy(update=llm_updates)})


def run_one_case(config: AppConfig, case: EvalCase) -> CaseExecution:
    start = time.perf_counter()
    try:
        result = classify_messages(config.llm, config.whatsapp.batch_size, config.topics, case.messages)
    except ClassificationError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        message = str(exc)
        return CaseExecution(
            case_id=case.id,
            passed=False,
            raw_passed=False,
            failures=(message,),
            raw_failures=(message,),
            latency_ms=elapsed_ms,
            input_tokens=0,
            output_tokens=0,
            raw_match_count=0,
            alert_count=0,
            error=message,
        )

    elapsed_ms = (time.perf_counter() - start) * 1000
    raw_matches = list(result.matches)
    alerts = build_alerts(
        config.topics,
        case.messages,
        result.matches,
        existing_alert_keys=set(),
        created_at="eval",
    )
    failures = evaluate_case(case, alerts)
    raw_failures = evaluate_case(case, raw_matches)
    return CaseExecution(
        case_id=case.id,
        passed=not failures,
        raw_passed=not raw_failures,
        failures=tuple(failures),
        raw_failures=tuple(raw_failures),
        latency_ms=elapsed_ms,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        raw_match_count=len(raw_matches),
        alert_count=len(alerts),
        error=None,
    )


def looks_like_schema_error(message: str) -> bool:
    """Heuristic: did the provider reject the strict JSON schema specifically?"""
    lowered = message.lower()
    needles = ("response_format", "json_schema", "json schema", "structured output", "structured_output")
    return any(needle in lowered for needle in needles)


def print_model_progress(summary: ModelRunSummary) -> None:
    passed = sum(1 for execution in summary.executions if execution.passed)
    raw_passed = sum(1 for execution in summary.executions if execution.raw_passed)
    errors = sum(1 for execution in summary.executions if execution.error)
    total = len(summary.executions)
    print(
        f"  {summary.slug}: pass={passed}/{total} raw={raw_passed}/{total} "
        f"errors={errors} mode={summary.structured_mode}"
    )


def print_comparison_table(summaries: list[ModelRunSummary], case_count: int) -> None:
    header = ("Model", "Mode", "Pass", "Raw", "p50 ms", "Tokens in", "Tokens out", "Errors")
    rows: list[tuple[str, ...]] = []
    for summary in summaries:
        passed = sum(1 for execution in summary.executions if execution.passed)
        raw_passed = sum(1 for execution in summary.executions if execution.raw_passed)
        errors = sum(1 for execution in summary.executions if execution.error)
        latencies = [execution.latency_ms for execution in summary.executions if execution.error is None]
        p50 = f"{int(statistics.median(latencies))}" if latencies else "-"
        input_tokens = sum(execution.input_tokens for execution in summary.executions)
        output_tokens = sum(execution.output_tokens for execution in summary.executions)
        rows.append(
            (
                summary.slug,
                summary.structured_mode,
                f"{passed}/{case_count}",
                f"{raw_passed}/{case_count}",
                p50,
                f"{input_tokens}",
                f"{output_tokens}",
                f"{errors}",
            )
        )
    print()
    render_table(header, rows)


def render_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [len(value) for value in header]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    sep = "  "
    print(sep.join(value.ljust(widths[index]) for index, value in enumerate(header)))
    print(sep.join("-" * widths[index] for index in range(len(header))))
    for row in rows:
        print(sep.join(value.ljust(widths[index]) for index, value in enumerate(row)))


def print_failure_details(summaries: list[ModelRunSummary]) -> None:
    for summary in summaries:
        broken_executions = [execution for execution in summary.executions if not execution.passed or execution.error]
        if not broken_executions:
            continue
        print(f"\n{summary.slug} ({summary.structured_mode}):")
        for execution in broken_executions:
            marker = "ERROR" if execution.error else ("FAIL" if not execution.passed else "RAW-FAIL")
            print(f"  [{marker}] {execution.case_id}")
            for failure in execution.failures:
                print(f"    - {failure}")
            if execution.raw_failures and tuple(execution.raw_failures) != tuple(execution.failures):
                print("    raw_failures:")
                for failure in execution.raw_failures:
                    print(f"      - {failure}")


def write_artifact(
    results_dir: Path,
    summaries: list[ModelRunSummary],
    cases: list[EvalCase],
    config: AppConfig,
) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = results_dir / f"compare_{timestamp}.json"
    payload = {
        "timestamp": timestamp,
        "config": {
            "topic_ids": sorted(topic.id for topic in config.topics),
            "batch_size": config.whatsapp.batch_size,
            "temperature": config.llm.temperature,
        },
        "case_ids": [case.id for case in cases],
        "model_runs": [serialize_summary(summary) for summary in summaries],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def serialize_summary(summary: ModelRunSummary) -> dict[str, Any]:
    return {
        "slug": summary.slug,
        "structured_mode": summary.structured_mode,
        "executions": [serialize_execution(execution) for execution in summary.executions],
    }


def serialize_execution(execution: CaseExecution) -> dict[str, Any]:
    return {
        "case_id": execution.case_id,
        "passed": execution.passed,
        "raw_passed": execution.raw_passed,
        "failures": list(execution.failures),
        "raw_failures": list(execution.raw_failures),
        "latency_ms": round(execution.latency_ms, 1),
        "input_tokens": execution.input_tokens,
        "output_tokens": execution.output_tokens,
        "raw_match_count": execution.raw_match_count,
        "alert_count": execution.alert_count,
        "error": execution.error,
    }


if __name__ == "__main__":
    raise SystemExit(main())
